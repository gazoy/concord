#!/usr/bin/env python3
"""Foliant on Avalanche Fuji, from nothing to a settled session.

    pip install "foliant-protocol[chain]" httpx
    python try_fuji.py

No wallet, no faucet and no configuration: the script makes a key, asks the demo server's tap to
fund it, and then does the thing Foliant is for — an orchestrator gives a worker a budget, the
worker pays for several API calls off chain, the chain refuses the one payment the budget does not
allow, and the whole session settles in one transaction. Every step prints the transaction so you
can check it on Snowtrace yourself.

Everything here is testnet. The key it writes to ~/.foliant-try.json is worth nothing.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SERVER = os.environ.get("FOLIANT_DEMO", "https://fuji.foliant.network")
STATE = Path(os.environ.get("FOLIANT_TRY_STATE", Path.home() / ".foliant-try.json"))
EXPLORERS = {43113: "https://testnet.snowtrace.io", 43114: "https://snowtrace.io"}
explorer = ""  # set from the chain the server is on; a private chain has none

try:
    import httpx
    from eth_account import Account
    from web3 import Web3
    from foliant.chain import ChainAgent, ChainHttpClient, ChainLedger, ChainPolicy
    from foliant.errors import PolicyViolation
except ImportError as e:
    sys.exit(f'{e}\n\nInstall the dependencies first:\n    pip install "foliant-protocol[chain]" httpx')

# amounts are in the token's smallest unit; this one has 6 decimals, like USDC
UNIT = 10 ** 6


def tokens(n: int) -> str:
    return f"{n / UNIT:,.2f}"


def step(n: int, what: str) -> None:
    print(f"\n\033[1m{n}. {what}\033[0m")


def link(kind: str, ident) -> str:
    """An explorer URL where there is an explorer, the bare identifier where there is not."""
    ident = ident if isinstance(ident, str) else "0x" + ident.hex()
    return f"{explorer}/{kind}/{ident}" if explorer else ident


def tx(label: str, h) -> None:
    print(f"   {label}: {link('tx', h)}")


def main() -> int:
    global explorer
    print(f"Foliant — {SERVER}")

    # ---------------------------------------------------------------- the server's terms
    step(1, "Ask the server what it offers")
    try:
        chain = httpx.get(f"{SERVER}/chain", timeout=30).json()
    except Exception as e:  # noqa: BLE001
        return fail(f"cannot reach {SERVER}: {e}", "The demo server may be down. The same script works "
                    "against your own: see demo/serve_chain.py in the repository.")
    price = chain["price"]
    explorer = EXPLORERS.get(chain["chainId"], "")
    print(f"   network   {chain['network']} (chain id {chain['chainId']})")
    print(f"   accounts  {link('address', chain['contracts']['accounts'])}")
    print(f"   price     {price} units per call, paid in {chain['token'][:10]}…")

    # ---------------------------------------------------------------- a key and some test funds
    step(2, "Get a key and ask the tap to fund it")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if "key" in state:
        acct = Account.from_key(state["key"])
        print(f"   reusing the key in {STATE}")
    else:
        acct = Account.create()
        state = {"key": acct.key.hex() if acct.key.hex().startswith("0x") else "0x" + acct.key.hex()}
        STATE.write_text(json.dumps(state))
        STATE.chmod(0o600)
        print(f"   made a new key, saved to {STATE}")
    print(f"   address   {acct.address}")

    # the server names its own contracts and node, so this script works against any Foliant server,
    # including one you run yourself
    c = chain["contracts"]
    L = ChainLedger(chain["rpc"], c["accounts"], c["channels"], c["pools"])
    token = L.token(chain["token"])
    if L.w3.eth.get_balance(acct.address) == 0:
        r = httpx.post(f"{SERVER}/tap", json={"address": acct.address}, timeout=180)
        if r.status_code != 200:
            return fail(f"the tap refused: {r.json().get('error', r.text)}",
                        "If it says the tap is dry or busy, try again later, or fund the address above "
                        "from https://core.app/tools/testnet-faucet and rerun.")
        for h in r.json()["transactions"]:
            tx("funded", h)
    else:
        print("   already funded")
    print(f"   balance   {L.w3.from_wei(L.w3.eth.get_balance(acct.address), 'ether')} AVAX, "
          f"{tokens(token.functions.balanceOf(acct.address).call())} tokens")

    # ---------------------------------------------------------------- the orchestrator's account
    step(3, "Register the orchestrator's account and fund it on chain")
    # the account the crew's budget hangs from: at most 500 per payment, 2000 in any hour
    boss = ChainAgent(L, state["key"])
    root = L.accounts.functions.accountId(acct.address, acct.address, 0).call()
    if L.accounts.functions.exists(root).call():
        boss.account_id = root
        print("   already registered")
    else:
        boss.register(ChainPolicy(per_tx_max=500 * UNIT, per_window_max=2000 * UNIT, window_secs=3600))
        print(f"   policy    500 per payment, 2000 per hour")
    held = L.accounts.functions.balanceOf(root, token.address).call()
    if held < 400 * UNIT:
        boss.deposit(token.address, 500 * UNIT)
        held = L.accounts.functions.balanceOf(root, token.address).call()
    print(f"   account   {'0x' + root.hex()[:16]}… holding {tokens(held)} tokens")

    # ---------------------------------------------------------------- delegate a worker
    step(4, "Give a worker its own budget inside the orchestrator's")
    # the same key operates both accounts here, to keep the demo to one wallet; in a real crew the
    # worker would hold its own key and the orchestrator would never see it
    worker_id = L.accounts.functions.childId(root, acct.address, 1).call()
    if L.accounts.functions.exists(worker_id).call():
        print("   already delegated")
    else:
        boss.delegate(acct.address, ChainPolicy(per_tx_max=100 * UNIT, per_window_max=300 * UNIT,
                                                window_secs=3600), token.address, 300 * UNIT, salt=1)
        print("   policy    100 per payment, 300 per hour — inside the orchestrator's, and checked against it")
    worker = ChainAgent(L, state["key"], account_id=worker_id)
    print(f"   worker    {'0x' + worker_id.hex()[:16]}… funded with "
          f"{tokens(L.accounts.functions.balanceOf(worker_id, token.address).call())} tokens")

    # ---------------------------------------------------------------- pay for some calls
    step(5, "Make three paid API calls")
    client = ChainHttpClient(worker, httpx.Client(timeout=60), default_deposit=100 * UNIT)
    for i in range(3):
        r = client.request("POST", f"{SERVER}/infer", content=f"call {i + 1}".encode())
        if r.status_code != 200:
            return fail(f"call {i + 1} was not served: {r.status_code} {r.text[:200]}")
        print(f"   call {i + 1}    paid {r.json()['paid']} units, answered {r.json()['echo']!r}")
    print(f"   {len(client.receipts)} signed receipts, and not one of those calls touched the chain")

    # ---------------------------------------------------------------- the budget bites
    step(6, "Try to commit more than the worker's budget allows")
    try:
        worker.open_channel(chain["provider"], token.address, 200 * UNIT, 3600, salt=7)
        print("   \033[31mthe chain allowed it — that is a bug, please open an issue\033[0m")
    except PolicyViolation as e:
        print(f"   refused by the contract: {e}")
        print("   nothing was sent and no gas was spent: the policy is checked before the transaction")

    # ---------------------------------------------------------------- settle
    step(7, "Settle the session — one transaction for all of it")
    # only the provider can settle on demand; a visitor waits for the timer, which is the honest
    # picture anyway — settlement is the provider's business, not the payer's
    out = None
    if os.environ.get("FOLIANT_SETTLE_KEY"):
        r = httpx.post(f"{SERVER}/settle", headers={"X-Settle-Key": os.environ["FOLIANT_SETTLE_KEY"]}, timeout=180)
        out = r.json() if r.status_code == 200 else None
    if out and out["transactions"]:
        print(f"   settled {out['settled']} units")
        for h in out["transactions"]:
            tx("settlement", h)
    else:
        print("   the public server settles on a timer, so this run's payments go out within ten minutes.")
        print(f"   watch the pool: {link('address', chain['contracts']['pools'])}")

    spent_root = L.accounts.functions.spentInWindow(root).call()
    spent_worker = L.accounts.functions.spentInWindow(worker_id).call()
    print(f"\n\033[1mWhat the chain now says\033[0m")
    print(f"   worker committed {tokens(spent_worker)} of its 300 this hour")
    print(f"   orchestrator shows {tokens(spent_root)} — the worker's commitment counted against it too")
    print("\nThat is the whole idea: a budget the worker cannot exceed, enforced where the money is,")
    print("and a session of calls that costs one transaction instead of one per call.")
    print(f"\nSpecification: https://github.com/gazoy/concord/blob/main/docs/spec/spending-policy.md")
    return 0


def fail(problem: str, hint: str = "") -> int:
    print(f"\n\033[31m{problem}\033[0m")
    if hint:
        print(hint)
    return 1


if __name__ == "__main__":
    sys.exit(main())
