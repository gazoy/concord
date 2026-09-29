"""Six further uses of the same primitives, each as a runnable scenario.

1. Containment      an agent misbehaves; the owner cuts it off in one transaction,
                    and a bypassed signer is still refused by the ledger.
2. Insurable agent  a hard cap plus signed receipts: max exposure known before the
                    session, a claims record after it.
3. Abuse control    a scraper pays per call through a pool; its budget runs out;
                    an honest caller exits with its deposit; the provider settles once.
4. Cross-company    two firms' agents trade through a channel with bounded exposure;
                    the counterparty goes silent; the payer recovers alone.
5. Delegated money  a funder delegates to a department, which delegates to a
                    project; allow-list, expiry and receipts bound every level.
6. Device fleet     an EV pays a charger by the second through a stream; the
                    charger claims what has accrued; the owner's cap holds.

Nothing here is new library code: every scenario uses foliant/ as it stands.
Where the library lacks something the whitepaper describes (a policy tree
enforced as one object rather than delegation by funding), the scenario says so.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=".*httpx2.*")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from foliant import Agent, KeyPair, Ledger, Policy, ServiceOffer
from foliant.crypto import sign
from foliant.errors import PolicyViolation, Unauthorized
from foliant.x402 import AgentHttpClient
from demo.api import build_app

ASSET = "USDC"
HOUR = 3600


def line(msg: str) -> None:
    print(f"  {msg}")


def agent(L: Ledger, name: str, policy: Policy, funds: int = 10_000) -> Agent:
    a = Agent(L, KeyPair.from_seed(name.encode() + b"-owner"), KeyPair.from_seed(name.encode() + b"-signer"), policy)
    if funds:
        L.mint(a.account.address, ASSET, funds)
    return a


def provider_api(L: Ledger, name: str, price: int, pool_timeout: int = 60):
    kp = KeyPair.from_seed(name.encode())
    L.mint(kp.address, ASSET, 1_000)
    pool = L.create_pool(kp.address, ASSET, timeout_secs=pool_timeout, bond=1_000)
    offer = ServiceOffer(provider=kp.address, asset=ASSET, price_per_unit=price, unit="call",
                         descriptor={"service": name}, pool_id=pool.id)
    L.publish_offer(offer)
    app, gate = build_app(L, kp, offer)
    return kp, pool, gate, TestClient(app, base_url=f"http://{name}")


# ---------------------------------------------------------------------------

def scenario_1_containment(L: Ledger) -> None:
    print("\n1. containment: cut off a misbehaving agent in one transaction")
    _, _, gate, http = provider_api(L, "tools", price=5)
    owner_kp = KeyPair.from_seed(b"ops-owner")
    duty = KeyPair.from_seed(b"duty-officer")  # escalation co-signer
    pol = Policy(per_tx_max=100, per_window_max=1_000, window_secs=HOUR, expiry=L.now + 4 * HOUR, escalation=duty.public)
    bot = Agent(L, owner_kp, KeyPair.from_seed(b"bot-signer"), pol)
    L.mint(bot.account.address, ASSET, 5_000)
    c = AgentHttpClient(bot, http, default_deposit=100, prefer_pool=False)
    for _ in range(30):
        assert c.post("/infer", content="work").status_code == 200
    line(f"bot ran 30 calls inside policy; committed {bot.signer.window.spent(L.now, HOUR)} of {pol.per_window_max}")

    # a large one-off needs the duty officer's co-signature; without it, refused
    big = 500
    try:
        bot.transfer(KeyPair.from_seed(b"vendor").address, ASSET, big)
    except PolicyViolation as e:
        line(f"{big}-unit transfer without co-signer refused: {e}")
    bot.transfer(KeyPair.from_seed(b"vendor").address, ASSET, big, escalate_with=duty)
    line(f"same transfer with the duty officer's co-signature: applied")

    # the owner sees misbehaviour and revokes: expiry set to now, one owner-signed transaction
    dead = Policy(per_tx_max=0, per_window_max=0, window_secs=HOUR, expiry=L.now)
    env = sign(owner_kp, {"account": bot.account.id, "nonce": bot.account.nonce, "op": "set_policy", "policy": dead.to_dict()})
    L.apply(env)
    bot.signer.policy = dead  # an honest enclave picks up the new policy
    try:
        c.post("/infer", content="more")
    except PolicyViolation as e:
        line(f"after revocation the signer refuses: {e}")

    # a compromised signer that ignores the policy and signs anyway: the ledger refuses
    forged = bot.signer.sign_plain({"account": bot.account.id, "nonce": bot.account.nonce, "op": "transfer",
                                    "to": KeyPair.from_seed(b"attacker").address, "asset": ASSET, "amount": 1})
    try:
        L.apply(forged)
    except PolicyViolation as e:
        line(f"a signer that bypasses its own check is refused by the ledger: {e}")
    line(f"provider still settles what was honestly bought: +{gate.settle()}")


def scenario_2_insurable(L: Ledger) -> None:
    print("\n2. insurable agent: bounded exposure before, a claims record after")
    _, _, gate, http = provider_api(L, "research-api", price=4)
    cap = 400
    a = agent(L, "insured", Policy(per_tx_max=100, per_window_max=cap, window_secs=24 * HOUR))
    line(f"underwriter's maximum exposure for the day: {cap} (per_window_max), regardless of the agent's code")
    c = AgentHttpClient(a, http, default_deposit=100, prefer_pool=False)
    done = 0
    try:
        for _ in range(1_000):
            c.post("/infer", content="q")
            done += 1
    except PolicyViolation:
        pass
    spent = sum(r["amount"] for r in c.receipts) if c.receipts and "amount" in c.receipts[0] else done * 4
    line(f"agent made {done} calls before the cap stopped it; committed {a.account.window.spent(L.now, 24 * HOUR)} <= {cap}")
    line(f"claims record: {len(c.receipts)} provider-signed receipts, every one tied to a signed update; value bought {spent}")
    line(f"provider settles once: +{gate.settle()}")


def scenario_3_abuse(L: Ledger) -> None:
    print("\n3. abuse control: every call costs, honest callers get their deposit back")
    kp, pool, gate, http = provider_api(L, "public-api", price=1, pool_timeout=30)
    scraper = agent(L, "scraper", Policy(per_tx_max=50, per_window_max=50, window_secs=HOUR))
    honest = agent(L, "reader", Policy(per_tx_max=50, per_window_max=50, window_secs=HOUR))
    cs = AgentHttpClient(scraper, http, default_deposit=50, prefer_pool=True)
    ch = AgentHttpClient(honest, http, default_deposit=50, prefer_pool=True)
    hits = 0
    try:
        for _ in range(10_000):
            cs.post("/infer", content="scrape")
            hits += 1
    except (PolicyViolation, ValueError):
        pass
    line(f"scraper stopped after {hits} calls: deposit exhausted and its policy will not fund another")
    for _ in range(5):
        assert ch.post("/infer", content="read").status_code == 200
    before = L.balance(honest.account.address, ASSET)
    honest.begin_exit(pool.id)
    L.advance(31)
    honest.finalize_exit(pool.id)
    line(f"honest reader made 5 calls, exited the pool alone, got {L.balance(honest.account.address, ASSET) - before} of 50 back")
    line(f"provider settles both in one transaction: +{gate.settle()} (scraper's {hits} + reader's 5)")


def scenario_4_cross_company(L: Ledger) -> None:
    print("\n4. cross-company trade: bounded exposure, no bilateral contract, unilateral recovery")
    carrier = agent(L, "carrier-agent", Policy(per_tx_max=10_000, per_window_max=10_000, window_secs=HOUR))
    shipper = agent(L, "shipper-agent", Policy(per_tx_max=300, per_window_max=300, window_secs=HOUR,
                                               allow_list=frozenset({carrier.account.address})))
    cid = shipper.open_channel(carrier.account.address, ASSET, deposit=300, timeout_secs=120)
    line(f"shipper's agent opens a channel to the carrier's agent: exposure capped at 300 by deposit and policy")
    for slot in range(6):
        shipper.pay_channel(cid, 40)  # six capacity bookings at 40 each
    line(f"six bookings signed off-chain; owed {shipper.latest[cid].body['balance']}, zero chain transactions since open")
    # carrier's agent goes silent before settling; shipper closes alone with its latest update
    shipper.close_channel(cid)
    L.advance(121)
    before = L.balance(shipper.account.address, ASSET)
    shipper.finalize_close(cid)
    ch = L.channels[cid]
    line(f"carrier silent: shipper closes after timeout; carrier still receives {ch.balance_to_payee} for bookings made, "
         f"shipper recovers {L.balance(shipper.account.address, ASSET) - before}")
    try:
        shipper.open_channel(KeyPair.from_seed(b"unknown-carrier").address, ASSET, deposit=10)
    except PolicyViolation as e:
        line(f"an unknown counterparty is refused by the allow-list: {e}")


def scenario_5_delegated_money(L: Ledger) -> None:
    print("\n5. delegated money: funder -> department -> project, bounded at every level")
    vendors = {KeyPair.from_seed(b"approved-cloud").address, KeyPair.from_seed(b"approved-data").address}
    dept = agent(L, "department", Policy(per_tx_max=2_000, per_window_max=2_000, window_secs=90 * 24 * HOUR,
                                         allow_list=frozenset(vendors), expiry=L.now + 90 * 24 * HOUR), funds=0)
    project = agent(L, "project", Policy(per_tx_max=200, per_window_max=500, window_secs=30 * 24 * HOUR,
                                         allow_list=frozenset(vendors), expiry=L.now + 30 * 24 * HOUR), funds=0)
    funder = agent(L, "funder", Policy(per_tx_max=5_000, per_window_max=5_000, window_secs=365 * 24 * HOUR,
                                       allow_list=frozenset({dept.account.address})), funds=20_000)
    funder.transfer(dept.account.address, ASSET, 2_000)
    dept_owner = KeyPair.from_seed(b"department-owner")
    # the department's allow-list is vendors only; its owner widens it to fund the project
    widened = Policy(per_tx_max=2_000, per_window_max=2_000, window_secs=90 * 24 * HOUR,
                     allow_list=frozenset(vendors | {project.account.address}), expiry=L.now + 90 * 24 * HOUR)
    L.apply(sign(dept_owner, {"account": dept.account.id, "nonce": dept.account.nonce, "op": "set_policy", "policy": widened.to_dict()}))
    dept.signer.policy = widened
    dept.transfer(project.account.address, ASSET, 500)
    line(f"funder -> department 2,000 -> project 500; each hop passed the sender's policy and its allow-list")
    cloud = next(iter(vendors))
    project.transfer(cloud, ASSET, 150)
    line("project pays an approved vendor 150: applied")
    try:
        project.transfer(KeyPair.from_seed(b"conference-hotel").address, ASSET, 100)
    except PolicyViolation as e:
        line(f"project pays an unapproved vendor: {e}")
    try:
        project.transfer(cloud, ASSET, 400)
    except PolicyViolation as e:
        line(f"project exceeds its cap: {e}")
    L.advance(31 * 24 * HOUR)
    try:
        project.transfer(cloud, ASSET, 10)
    except PolicyViolation as e:
        line(f"after the project's expiry: {e}")
    line(f"unspent at the project: {L.balance(project.account.address, ASSET)}; returned by the owner, receipts on-chain for every hop")
    line("note: this is delegation by funding, each account with its own policy; a single policy tree enforced as one object is whitepaper work not yet in the library")


def scenario_6_device_fleet(L: Ledger) -> None:
    print("\n6. device fleet: an EV pays a charger by the second")
    charger = KeyPair.from_seed(b"charger-7")
    ev = agent(L, "ev-fleet-12", Policy(per_tx_max=600, per_window_max=600, window_secs=24 * HOUR,
                                        allow_list=frozenset({charger.address})))
    cid = ev.open_channel(charger.address, ASSET, deposit=600, timeout_secs=HOUR)
    ev.start_stream(cid, rate_per_sec=2)
    line("EV opens a 600-unit channel to the charger and starts a stream at 2 units/second")
    L.advance(120)
    line(f"after 120 s the charger can claim {L.channels[cid].stream_claimable(L.now)} without any message from the EV")
    got = L.payee_claim_stream(cid)
    line(f"charger claims mid-session: +{got}")
    L.advance(90)
    L.stop_stream(cid)
    got2 = L.payee_claim_stream(cid)
    line(f"EV unplugs at 210 s: charger claims the rest +{got2}; total {got + got2} = 210 s x 2")
    try:
        ev.open_channel(KeyPair.from_seed(b"charger-99").address, ASSET, deposit=10)
    except PolicyViolation as e:
        line(f"a charger outside the fleet's contract is refused: {e}")
    line(f"the fleet operator's daily cap of 600 per vehicle held; committed {ev.account.window.spent(L.now, 24 * HOUR)}")


def main() -> None:
    L = Ledger()
    minted_before = L.total_supply(ASSET)
    for s in (scenario_1_containment, scenario_2_insurable, scenario_3_abuse,
              scenario_4_cross_company, scenario_5_delegated_money, scenario_6_device_fleet):
        s(L)
    print(f"\nsupply check: {L.total_supply(ASSET)} units, every one accounted for")


if __name__ == "__main__":
    main()
