# Try Foliant on Avalanche Fuji

Ten minutes, no wallet and no faucet. You will give a worker agent an on-chain spending budget,
commit part of it to a provider in one transaction, pay for twenty-five API calls without touching
the chain at all, and watch the contract refuse the one commitment the budget does not allow.

Everything is testnet. Nothing here can cost you anything.

## Run it

Python 3.11 or newer. The virtual environment is not optional on Debian and Ubuntu, where a system
`pip install` is refused.

```bash
python3 -m venv venv && . venv/bin/activate
pip install "foliant-protocol[chain]" httpx
curl -fsSLO https://raw.githubusercontent.com/gazoy/concord/main/examples/try_fuji.py
python try_fuji.py
```

## What it prints

```
1. Ask the server what it offers
   network   avalanche-fuji (chain id 43113)
   price     0.10 per call

2. Get a key and ask the tap to fund it
   funded: https://testnet.snowtrace.io/tx/0x…
   holding   0.02 AVAX for gas, 1,000.00 tokens to spend

3. Register the orchestrator's account and fund it on chain
   policy    500.00 per payment, 2,000.00 per hour
   registered: https://testnet.snowtrace.io/tx/0x…

4. Give a worker its own budget inside the orchestrator's
   policy    20.00 per payment, 60.00 per hour
   delegated: https://testnet.snowtrace.io/tx/0x…

5. Commit the worker's budget to the provider's pool — one transaction
   joined: https://testnet.snowtrace.io/tx/0x…

6. Make 25 paid API calls — none of these touch the chain
   25 signed receipts, 2.50 paid, and 0 transactions sent

7. Try to commit more than the worker's budget allows
   refused by the contract: PolicyViolation: amount exceeds per_tx_max

8. Settlement — one transaction for the whole session
```

Every hash is a link, so none of it has to be taken on trust.

## What each step is actually showing

**Step 4 is the part that is hard to do anywhere else.** The worker gets its own account, its own
key and its own policy, created by the orchestrator in one transaction with no human in the loop.
Its policy sits inside the orchestrator's on every axis — and, more importantly, every payment it
makes is checked against its own policy *and every ancestor's*, all or nothing. So a worker cannot
exceed its budget, a crew of workers cannot exceed the orchestrator's, and tightening the
orchestrator's policy binds every worker already created under it, at their next payment. That last
property is [specified](spec/spending-policy.md) in §4.2 and tested by the conformance vectors; it
is the reason the budget is a bound rather than a convention.

**Step 5 is the payment the policy sees.** Foliant checks *committed* value: joining the pool
commits 20 tokens, and that single transaction is what the policy tree authorises. The calls that
draw on it afterwards cannot exceed the deposit, so they need no further check.

**Step 6 touches no chain at all.** Twenty-five HTTP 402 exchanges in the x402 wire format: the
worker signs an increased balance, the server verifies it against the deposit and serves the
request. The script asserts that zero transactions were sent during this step, so the claim is
checked rather than asserted. Two thousand calls would cost the same on-chain as twenty-five.

**Step 7 is the enforcement, and it is the step worth reading the code for.** The policy is a
precondition inside `AgentAccounts.commit`, which `PaymentChannels.open` must call. The attempt
therefore fails when the transaction is priced, before anything is broadcast — the SDK raises
`PolicyViolation` carrying the contract's own revert string. It is not a wrapper the agent's code
can route around by calling a different method.

**Step 8 is the economics.** One settlement for the session rather than one transaction per call.
The [cost report](fuji-cost-report.md) measures the on-chain side at roughly 335k gas for a pooled
session against about 75k per call paid individually, so twenty-five calls is comfortably past the
break-even of five to seven. Settlement is the provider's business, not the payer's, so the demo
server does it on a timer: your calls go on chain within ten minutes of the run.

## Then what

- The [spending-policy specification](spec/spending-policy.md) — the policy tree as a
  chain-agnostic draft with a JSON schema, 115 conformance vectors and a binding to the plain x402
  `exact` scheme, with an [independent review](spec/REVIEW-1.md) recording every finding and its
  resolution.
- From a framework: [`langchain-foliant`](https://pypi.org/project/langchain-foliant/) is a listed
  LangChain integration; `foliant-client` and `elizaos-plugin-foliant` are on npm.
- The [contracts](https://github.com/gazoy/concord/tree/main/contracts) and their
  [three audits](https://github.com/gazoy/concord/tree/main/contracts/audits).

## Against your own server

The script asks the server which contracts and which node it uses, so it runs against any Foliant
server:

```bash
FOLIANT_DEMO=http://localhost:8402 python try_fuji.py
```

[`deploy/README.md`](https://github.com/gazoy/concord/tree/main/deploy) sets one up on a small box
in about fifteen minutes.

## If something goes wrong

| What it says | What it means |
|---|---|
| `error: externally-managed-environment` | `pip` on Debian or Ubuntu refusing to install system-wide. Use the virtual environment in the commands above. |
| `this client has used its share of the tap for today` | Three addresses per visitor per day. A new key will not help; fund an address yourself at [the Core faucet](https://core.app/tools/testnet-faucet) and run again. |
| `the tap is serving someone else` | Someone else is being funded this second. Run it again. |
| `the tap is busy; try again in an hour` or `the tap is dry` | The demo wallet is rate-limited or needs refilling. Please [open an issue](https://github.com/gazoy/concord/issues). |
| `cannot reach the chain node` | The script talks to two hosts: the demo server and an Avalanche RPC node. A proxy may allow one and not the other. |
| `stale update` during step 6 | An earlier run left an unsettled claim. Wait for the provider's next settlement, then run again. |
| The second run says the account is nearly empty | One tap funds one address once. Delete `~/.foliant-try.json` for a new key, or top the address up from the faucet. |

That file, `~/.foliant-try.json`, holds the throwaway private key so a second run can reuse the
funds. It is testnet-only and worth nothing; delete it whenever you like.

Anything else — including anything that worked but read badly — please
[open an issue](https://github.com/gazoy/concord/issues). This page is meant to work first time for
someone who has never seen the project, and reports of where it does not are the most useful thing
you can send.
