"""End-to-end demo: agents pay a metered API per call; the API settles in one batch.

Scenario A: one agent, one channel, 20 calls, one settlement.
Scenario B: three agents share the provider's pool, 10 calls each, one settlement.
Scenario C: policy enforcement — the enclave refuses to sign past per_window_max.
Scenario D: unilateral exit from the pool after the coordinator goes silent.

Runs in-process (httpx ASGI transport); no network, no chain daemon.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=".*httpx2.*")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from concord import Agent, KeyPair, Ledger, Policy, ServiceOffer
from concord.errors import PolicyViolation
from concord.x402 import AgentHttpClient
from demo.api import build_app

ASSET = "USDC"  # smallest unit; think micro-dollars


def line(msg: str) -> None:
    print(f"  {msg}")


def main() -> None:
    L = Ledger()
    provider = KeyPair.from_seed(b"provider")
    L.mint(provider.address, ASSET, 1_000)  # for the pool bond

    pool = L.create_pool(provider.address, ASSET, timeout_secs=60, bond=1_000)
    offer = ServiceOffer(provider=provider.address, asset=ASSET, price_per_unit=3, unit="call",
                         descriptor={"model": "echo-1"}, pool_id=pool.id)
    L.publish_offer(offer)
    app, gate = build_app(L, provider, offer)
    http = TestClient(app, base_url="http://api")  # an httpx.Client over ASGI, in-process

    policy = Policy(per_tx_max=500, per_window_max=200, window_secs=3600)

    def make_agent(name: str) -> Agent:
        a = Agent(L, KeyPair.from_seed(name.encode() + b"-owner"), KeyPair.from_seed(name.encode() + b"-signer"), policy)
        L.mint(a.account.address, ASSET, 10_000)
        return a

    print("\nA. one agent, one channel, 20 calls")
    alice = make_agent("alice")
    client = AgentHttpClient(alice, http, default_deposit=100, prefer_pool=False)
    for i in range(20):
        r = client.post("/infer", content=f"prompt {i}")
        assert r.status_code == 200, r.text
    ch = next(c for c in L.channels.values() if c.payer_account == alice.account.id)
    line(f"channel {ch.id[:8]}: deposit {ch.deposit}, on-chain settled {ch.balance_to_payee}, off-chain owed {alice.latest[ch.id].body['balance']}")
    line(f"chain transfers so far: {len(L.log)}  (open only; no per-call transactions)")
    paid = gate.settle()
    line(f"provider settles once: +{paid} {ASSET}; provider balance {L.balance(provider.address, ASSET)}; receipts held by agent: {len(client.receipts)}")

    print("\nB. three agents share the provider's pool, 10 calls each")
    agents = [make_agent(n) for n in ("bob", "carol", "dave")]
    clients = [AgentHttpClient(a, http, default_deposit=100, prefer_pool=True) for a in agents]
    for i in range(10):
        for c in clients:
            assert c.post("/infer", content=f"q{i}").status_code == 200
    line(f"pool members: {len(pool.members)}; on-chain paid: {[m.paid for m in pool.members.values()]}")
    before = len(L.log)
    paid = gate.settle()
    line(f"one settlement transaction: +{paid} {ASSET} across 3 members; on-chain paid now {[m.paid for m in pool.members.values()]}; transfers added: {len(L.log) - before}")

    print("\nC. policy: per_window_max 200 of committed value stops the enclave signing")
    erin = make_agent("erin")
    c = AgentHttpClient(erin, http, default_deposit=100, prefer_pool=False)
    ok = 0
    try:
        for _ in range(100):
            c.post("/infer", content="x")
            ok += 1
    except PolicyViolation as e:
        line(f"after {ok} calls (two 100-unit channel deposits) the signer refused a third deposit: {e}")
    line(f"committed in window per signer: {erin.signer.window.spent(L.now, 3600)}; per ledger: {erin.account.window.spent(L.now, 3600)}")

    print("\nD. unilateral exit: coordinator goes silent, bob leaves the pool")
    bob = agents[0]
    claim = pool.members[bob.account.id]
    bal_before = L.balance(bob.account.address, ASSET)
    bob.begin_exit(pool.id)  # submits bob's own latest update
    line(f"exit opened at t+{pool.timeout_secs}s; bob's paid on-chain {claim.paid} of deposit {claim.deposit}")
    L.advance(61)
    bob.finalize_exit(pool.id)
    line(f"bob refunded {L.balance(bob.account.address, ASSET) - bal_before}; exited={claim.exited}")

    print(f"\nsupply check: {L.total_supply(ASSET)} == minted {1_000 + 10_000 * 5}")
    assert L.total_supply(ASSET) == 1_000 + 10_000 * 5


if __name__ == "__main__":
    main()
