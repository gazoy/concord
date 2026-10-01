#!/usr/bin/env python3
"""Foliant on Avalanche Fuji, from nothing to a settled session.

    python3 -m venv venv && . venv/bin/activate     # Python 3.11 or newer
    pip install "foliant-protocol[chain]" httpx
    python try_fuji.py

No wallet, no faucet and no configuration: the script makes a key, asks the demo server's tap to
fund it, and then does the thing Foliant is for. An orchestrator gives a worker a budget; the
worker commits part of it to a provider's pool in one transaction; it then pays for twenty-five API
calls without touching the chain at all; the chain refuses the one commitment the budget does not
allow; and the provider settles the whole session in a single transaction.

Every transaction is printed with a link, so none of it has to be taken on trust.

Everything here is testnet. The key written to ~/.foliant-try.json is worth nothing, but it is a
private key: the script keeps it so a second run can reuse the funds, and you can delete it freely.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SERVER = os.environ.get("FOLIANT_DEMO", "https://fuji.foliant.network").rstrip("/")
STATE = Path(os.environ.get("FOLIANT_TRY_STATE", Path.home() / ".foliant-try.json"))
CALLS = int(os.environ.get("FOLIANT_TRY_CALLS", "25"))
# testnets and private chains only: this script funds itself from a tap and is not written to be
# careful with anything of value. Avalanche mainnet (43114) is deliberately absent.
EXPLORERS = {43113: "https://testnet.snowtrace.io", 31337: ""}
explorer = ""

try:
    import httpx
    from eth_account import Account
    from foliant.chain import ChainAgent, ChainHttpClient, ChainLedger, ChainPolicy
    from foliant.errors import FoliantError, PolicyViolation
except ImportError as e:
    sys.exit(f'{e}\n\nInstall the dependencies first, ideally in a virtual environment:\n'
             f'    python3 -m venv venv && . venv/bin/activate\n'
             f'    pip install "foliant-protocol[chain]" httpx')

UNIT = 10 ** 6          # the demo token has 6 decimals, like USDC
BOLD = "\033[1m" if sys.stdout.isatty() else ""
RED = "\033[31m" if sys.stdout.isatty() else ""
OFF = "\033[0m" if sys.stdout.isatty() else ""


def fail(problem: str, hint: str = "") -> int:
    print(f"\n{RED}{problem}{OFF}")
    if hint:
        print(hint)
    return 1


def amount(n: int) -> str:
    """Token amounts, always in tokens, so nothing in this script is quoted at two scales."""
    return f"{n / UNIT:,.2f}"


def step(n: int, what: str) -> None:
    print(f"\n{BOLD}{n}. {what}{OFF}")


def link(kind: str, ident) -> str:
    ident = ident if isinstance(ident, str) else "0x" + ident.hex()
    return f"{explorer}/{kind}/{ident}" if explorer else ident


def record(agent):
    """The list of transaction hashes this agent broadcasts, so every one can be printed.

    ChainAgent keeps such a list itself in versions after 0.1.1; where it does not, this wraps the
    one method that sends, so the script works with the package as published either way.
    """
    sent = getattr(agent, "sent", None)
    if sent is None:
        sent = agent.sent = []
        inner = agent._send

        def _send(*a, **kw):
            r = inner(*a, **kw)
            sent.append("0x" + r["transactionHash"].hex())
            return r

        agent._send = _send
    return sent


def show(sent: list, label: str, first: int) -> int:
    """Print every transaction sent since `first`, and return the new mark."""
    for h in sent[first:]:
        print(f"   {label}: {link('tx', h)}")
    return len(sent)


def main() -> int:
    global explorer
    print(f"Foliant — {SERVER}")

    # ---------------------------------------------------------------- the server's terms
    step(1, "Ask the server what it offers")
    try:
        chain = httpx.get(f"{SERVER}/chain", timeout=30).json()
    except Exception as e:  # noqa: BLE001
        return fail(f"cannot reach {SERVER}: {e}",
                    "The demo server may be down, or a proxy may be in the way. The same script runs\n"
                    "against a server of your own: see deploy/README.md in the repository.")
    if chain["chainId"] not in EXPLORERS:
        return fail(f"this script only runs against a testnet; the server is on chain {chain['chainId']}.",
                    "It funds itself from a tap and takes no care with anything of value.")
    explorer = EXPLORERS[chain["chainId"]]
    price = chain["price"]
    print(f"   network   {chain['network']} (chain id {chain['chainId']})")
    print(f"   node      {chain['rpc']}")
    print(f"   accounts  {link('address', chain['contracts']['accounts'])}")
    print(f"   price     {amount(price)} per call")

    # the server names its own contracts and node, so this script works against any Foliant server
    c = chain["contracts"]
    try:
        L = ChainLedger(chain["rpc"], c["accounts"], c["channels"], c["pools"])
        token = L.token(chain["token"])
    except Exception as e:  # noqa: BLE001
        return fail(f"cannot reach the chain node at {chain['rpc']}: {e}",
                    "That is a different host from the server itself; a proxy may allow one and not the other.")

    # ---------------------------------------------------------------- a key and some test funds
    step(2, "Get a key and ask the tap to fund it")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if "key" in state:
        acct = Account.from_key(state["key"])
        print(f"   reusing the key in {STATE}")
    else:
        acct = Account.create()
        k = acct.key.hex()
        state = {"key": k if k.startswith("0x") else "0x" + k, "run": 0}
        STATE.touch(mode=0o600)
        STATE.write_text(json.dumps(state))
        print(f"   made a new key, saved to {STATE} (testnet, worth nothing, delete it freely)")
    print(f"   address   {acct.address}")

    if L.w3.eth.get_balance(acct.address) == 0:
        r = httpx.post(f"{SERVER}/tap", json={"address": acct.address}, timeout=180)
        if r.status_code != 200:
            return fail(f"the tap refused: {r.json().get('error', r.text)}",
                        "The tap funds three addresses per visitor per day. You can fund the address above\n"
                        "yourself at https://core.app/tools/testnet-faucet and run this again.")
        for h in r.json()["transactions"]:
            print(f"   funded: {link('tx', h)}")
    else:
        print("   already funded from an earlier run")
    print(f"   holding   {L.w3.from_wei(L.w3.eth.get_balance(acct.address), 'ether')} AVAX for gas, "
          f"{amount(token.functions.balanceOf(acct.address).call())} tokens to spend")

    # ---------------------------------------------------------------- the orchestrator's account
    step(3, "Register the orchestrator's account and fund it on chain")
    boss = ChainAgent(L, state["key"])
    boss_sent = record(boss)
    mark = 0
    root = L.accounts.functions.accountId(acct.address, acct.address, 0).call()
    root_policy = ChainPolicy(per_tx_max=500 * UNIT, per_window_max=2000 * UNIT, window_secs=3600)
    if L.accounts.functions.exists(root).call():
        boss.account_id = root
        print("   already registered by an earlier run")
    else:
        boss.register(root_policy)
        print(f"   policy    {amount(root_policy.per_tx_max)} per payment, "
              f"{amount(root_policy.per_window_max)} per hour")
        mark = show(boss_sent, "registered", mark)
    held = L.accounts.functions.balanceOf(root, token.address).call()
    if held < 50 * UNIT:  # only when it is actually short, so repeated runs do not keep depositing
        wallet = token.functions.balanceOf(acct.address).call()
        if wallet < 50 * UNIT:
            return fail("the orchestrator's account and the wallet are both nearly empty.",
                        f"Delete {STATE} to start over with a new key, or send test tokens to {acct.address}.")
        boss.deposit(token.address, min(wallet, 500 * UNIT))
        mark = show(boss_sent, "deposited", mark)
        held = L.accounts.functions.balanceOf(root, token.address).call()
    print(f"   account   0x{root.hex()[:16]}… holding {amount(held)} tokens")

    # ---------------------------------------------------------------- delegate a worker
    step(4, "Give a worker its own budget inside the orchestrator's")
    # a fresh worker each run, so a run never inherits an unsettled claim from the last one. The same
    # key operates both accounts here to keep the demo to one wallet; in a real crew the worker holds
    # its own key and the orchestrator never sees it.
    state["run"] = state.get("run", 0) + 1
    STATE.write_text(json.dumps(state))
    # the worker is funded with more than it may commit at once, so that step 7's refusal is the
    # policy refusing and not simply an empty account
    budget = 20 * UNIT
    worker_policy = ChainPolicy(per_tx_max=budget, per_window_max=budget * 3, window_secs=3600)
    worker_id = boss.delegate(acct.address, worker_policy, token.address, budget * 3, salt=state["run"])
    print(f"   policy    {amount(worker_policy.per_tx_max)} per payment, "
          f"{amount(worker_policy.per_window_max)} per hour — inside the orchestrator's, and checked against it")
    mark = show(boss_sent, "delegated", mark)
    worker = ChainAgent(L, state["key"], account_id=worker_id)
    worker_sent = record(worker)
    print(f"   worker    funded with {amount(L.accounts.functions.balanceOf(worker_id, token.address).call())} tokens")

    # ---------------------------------------------------------------- commit to the pool
    step(5, "Commit the worker's budget to the provider's pool — one transaction")
    # this is the spend the policy sees: committed value, checked against the worker's policy and
    # every ancestor's. The calls that draw on it are bounded by the deposit and are not checked again.
    pool = bytes.fromhex(chain["poolId"][2:])
    try:
        worker.join_pool(pool, budget)
    except PolicyViolation as e:
        return fail(f"the chain refused the commitment: {e}")
    show(worker_sent, "joined", 0)
    print(f"   committed {amount(budget)} — this is the payment the policy checks")

    # ---------------------------------------------------------------- pay for calls
    step(6, f"Make {CALLS} paid API calls — none of these touch the chain")
    client = ChainHttpClient(worker, httpx.Client(timeout=60), default_deposit=budget)
    before = len(worker_sent)
    for i in range(CALLS):
        r = client.request("POST", f"{SERVER}/infer", content=f"call {i + 1}".encode())
        if r.status_code != 200:
            return fail(f"call {i + 1} was not served: {r.status_code} {r.text[:300]}",
                        "If this says 'stale update', a previous run left an unsettled claim; wait for the\n"
                        "provider's next settlement and try again.")
        if i < 3 or i == CALLS - 1:
            print(f"   call {i + 1:<3} paid {amount(r.json()['paid'])}, answered {r.json()['echo']!r}")
        elif i == 3:
            print(f"   …")
    assert len(worker_sent) == before, "a call sent a transaction; that would be a bug"
    print(f"   {len(client.receipts)} signed receipts, {amount(CALLS * price)} paid, "
          f"and {len(worker_sent) - before} transactions sent")

    # ---------------------------------------------------------------- the budget bites
    step(7, "Try to commit more than the worker's budget allows")
    # the worker holds enough to do this; what stops it is the policy, which is the point
    try:
        over = budget + budget // 2   # half as much again as the worker's per-payment cap
        worker.open_channel(chain["provider"], token.address, over, 3600, salt=state["run"])
        print(f"   {RED}the chain allowed it — that is a bug, please open an issue{OFF}")
    except PolicyViolation as e:
        print(f"   refused by the contract: {e}")
        print("   the policy is a precondition of the contract call, so the attempt fails when the")
        print("   transaction is priced and never reaches the chain: nothing sent, no gas spent")

    # ---------------------------------------------------------------- settle
    step(8, "Settlement — one transaction for the whole session")
    out = None
    if os.environ.get("FOLIANT_SETTLE_KEY"):  # the provider's own key; a visitor will not have one
        r = httpx.post(f"{SERVER}/settle", headers={"X-Settle-Key": os.environ["FOLIANT_SETTLE_KEY"]}, timeout=180)
        out = r.json() if r.status_code == 200 else None
    if out and out["transactions"]:
        print(f"   settled {amount(out['settled'])} in {len(out['transactions'])} transaction(s)")
        for h in out["transactions"]:
            print(f"   settlement: {link('tx', h)}")
    else:
        print("   settling is the provider's business, not the payer's, and this server does it on a")
        print(f"   timer. Your {CALLS} calls will go on chain in one transaction at its next tick.")
        print(f"   watch the pool: {link('address', chain['contracts']['pools'])}")

    # ---------------------------------------------------------------- what the chain says
    spent_root = L.accounts.functions.spentInWindow(root).call()
    spent_worker = L.accounts.functions.spentInWindow(worker_id).call()
    print(f"\n{BOLD}What the chain now says{OFF}")
    print(f"   worker committed   {amount(spent_worker)} of its {amount(worker_policy.per_window_max)} this hour")
    print(f"   orchestrator shows {amount(spent_root)} — a worker's commitment counts against its parent too")
    print(f"\n{CALLS} paid calls cost {len(worker_sent)} transaction to set up and one to settle.")
    print("A worker cannot exceed its budget, and no crew can exceed the orchestrator's, because")
    print("every payment is checked against every account above it before any value moves.")
    print("\nHow it works: https://github.com/gazoy/concord/blob/main/docs/spec/spending-policy.md")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FoliantError as e:
        sys.exit(fail(f"{type(e).__name__}: {e}"))
    except KeyboardInterrupt:
        sys.exit(130)
