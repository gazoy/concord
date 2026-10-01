# Try Foliant on Avalanche Fuji

Ten minutes, no wallet, no faucet, no configuration. You will give a worker agent an on-chain
spending budget, watch it pay for API calls without touching the chain, watch the chain refuse the
one payment the budget does not allow, and settle the whole session in a single transaction.

Everything is testnet. Nothing here can cost you anything.

## Run it

```bash
pip install "foliant-protocol[chain]" httpx
curl -O https://raw.githubusercontent.com/gazoy/concord/main/examples/try_fuji.py
python try_fuji.py
```

The script makes a throwaway key, asks the demo server's tap to fund it with test AVAX and test
tokens, and runs the session. It prints a Snowtrace link for every transaction, so nothing here has
to be taken on trust.

## What you should see

```
1. Ask the server what it offers
   network   avalanche-fuji (chain id 43113)
   price     3 units per call

2. Get a key and ask the tap to fund it
   funded: https://testnet.snowtrace.io/tx/0x…

3. Register the orchestrator's account and fund it on chain
   policy    500 per payment, 2000 per hour

4. Give a worker its own budget inside the orchestrator's
   policy    100 per payment, 300 per hour

5. Make three paid API calls
   call 1    paid 3 units
   call 2    paid 3 units
   call 3    paid 3 units
   3 signed receipts, and not one of those calls touched the chain

6. Try to commit more than the worker's budget allows
   refused by the contract: PolicyViolation: amount exceeds per_tx_max

7. Settle the session — one transaction for all of it
```

## What each step is actually showing

**Step 4 is the part that is hard to do anywhere else.** The worker gets its own account with its
own key and its own policy, created by the orchestrator in one transaction, without a human. The
worker's policy sits inside the orchestrator's on every axis, and — this is the part that matters —
every payment the worker makes is checked against *its own* policy and against *every ancestor's*,
all or nothing. A worker cannot exceed its budget, and a crew of workers cannot exceed the
orchestrator's, even if the orchestrator tightens its policy after the workers were created.

**Step 5 touches no chain at all.** The three calls are HTTP 402 exchanges in the x402 wire format:
the worker signs an updated balance, the server verifies it against the deposit and serves the
request. Thousands of calls cost the same on-chain as three.

**Step 6 is the enforcement.** Opening a channel commits value, so it is checked against the policy
tree. The attempt fails at estimate time, which means no transaction is sent and no gas is spent —
the policy is not a wrapper the agent's code can route around, it is the contract's precondition.

**Step 7 is the economics.** One settlement for the session, not one transaction per call. The
[measured cost report](fuji-cost-report.md) puts the break-even at five to seven calls and the
saving above 90% at a hundred.

## Then what

- Read the [spending-policy specification](spec/spending-policy.md) — the policy tree as a
  chain-agnostic draft with a JSON schema, 115 conformance vectors, and a binding to the plain x402
  `exact` scheme. It has had an [independent review](spec/REVIEW-1.md) with every finding recorded.
- Use it from a framework: [`langchain-foliant`](https://pypi.org/project/langchain-foliant/) is a
  listed LangChain integration; `elizaos-plugin-foliant` and `foliant-client` are on npm.
- Read the [contracts](https://github.com/gazoy/concord/tree/main/contracts) and the
  [three audits](https://github.com/gazoy/concord/tree/main/contracts/audits).

## Running it against your own server

The script asks the server which contracts and which node it uses, so it works against any Foliant
server, not just the public demo:

```bash
FOLIANT_DEMO=http://localhost:8402 python try_fuji.py
```

[`deploy/README.md`](https://github.com/gazoy/concord/tree/main/deploy) sets one up on a small box
in about fifteen minutes.

## If something goes wrong

**"the tap refused: this client has used its share of the tap for today"** — three addresses per
client per day. Delete `~/.foliant-try.json` and you will get a new key but the same refusal; wait,
or fund an address yourself at <https://core.app/tools/testnet-faucet> and rerun.

**"the tap is dry"** — the demo wallet needs refilling. Please
[open an issue](https://github.com/gazoy/concord/issues) and it will be topped up.

**"cannot reach https://fuji.foliant.network"** — the demo server is down. The script works against
your own server, as above.

Anything else, including anything that worked but read badly: please
[open an issue](https://github.com/gazoy/concord/issues). This page is meant to work first time for
someone who has never seen the project, and reports of where it does not are the most useful thing
you can send.
