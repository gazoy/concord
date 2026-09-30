# Foliant — reference implementation (Python)

Executable specification of the Foliant protocol: budgets and settlement for
AI agent crews. Agent accounts with spending policies the agent's code cannot
bypass, arranged in a tree so a whole crew shares one bound, TEE-style attestation, payment channels and streams, Ark-style pooled
channels a whole crew pays through, HTTP 402 metering in the x402 wire format,
and the compute-and-data market objects (whitepaper §6).

Foliant is a protocol, not a chain: on-chain contracts, an x402 payment scheme
and an SDK, deployable on chains agents already use. First targets are Base and
Avalanche. The whitepaper also describes a sovereign chain the protocol could
grow into if adoption warrants it; that is a direction, not the product. §11a
of the whitepaper records the prior art (Tempo, the x402 batch-settlement
scheme and others) and what Foliant adds.

This is a **spec plus demo**, not a deployment. It runs in one process with an
in-memory ledger so the state machines and their invariants can be read,
changed and tested quickly. The Solidity port implements the same state
machines as contracts; the tests here become its conformance suite.

## Name

The project was called Concord until 28 September 2026; the name was changed to Foliant after a UK trade-mark search. Older links, issues and messages that say Concord refer to this project.

## Documents

- Website: [foliant.network](https://foliant.network) (served from `docs/` via GitHub Pages)
- [Whitepaper v0.1](docs/whitepaper.md) — the full design (§6 is what this repo implements)
- [Design, feasibility and cost study](docs/study.md) — chain survey, build routes, costs, risks
- [Agent Spending Policy, draft 0.1](docs/spec/spending-policy.md) — the policy tree as a chain-agnostic specification: schema, 115 conformance vectors run against both references, an x402 `exact` binding, and an [independent review](docs/spec/REVIEW-1.md)

## Demo

![Foliant demo](demo/video/foliant-demo.gif)

[Two-minute narrated walkthrough (MP4)](demo/video/foliant-demo-voiced.mp4) of the first four scenarios, by the author (scenario E, the crew, was added afterwards and is not in the recording).

`python demo/make_video.py` regenerates the captioned video, GIF and voiceover script in `demo/video/`; pass the voiceover's section boundaries in seconds to re-time the captions.

## Run

```bash
pip install -r requirements.txt
python demo/run_demo.py     # five scenarios: channel, pool, policy, exit, crew; ~1 s
python demo/run_uses.py     # six further uses: containment, insurable agent, abuse control,
                            # cross-company trade, delegated money, device fleet
pytest                      # 34 tests incl. property-based invariants
python sim/crosschain.py --quick   # cross-ledger budget-tree simulation, smoke run
```

## On Avalanche Fuji

The contracts are deployed on Avalanche Fuji (chain 43113): AgentAccounts [`0xDB6940FBD9Dcf8AAD1aBB521B4b6790D0b579c92`](https://testnet.snowtrace.io/address/0xDB6940FBD9Dcf8AAD1aBB521B4b6790D0b579c92), PaymentChannels [`0xa857d5EF74F63fd10BD786bf2d66EaA53736c976`](https://testnet.snowtrace.io/address/0xa857d5EF74F63fd10BD786bf2d66EaA53736c976), Pools [`0x26665c7Ad0a4272F87D1949745dD4B4743F53662`](https://testnet.snowtrace.io/address/0x26665c7Ad0a4272F87D1949745dD4B4743F53662). `ChainLedger.for_network("avalanche-fuji")` in `foliant.chain` targets them; `demo/serve_chain.py` runs the metered API against them. Measured gas and the cost of a session: [docs/fuji-cost-report.md](docs/fuji-cost-report.md). Audits: [`contracts/audits/`](contracts/audits/).

## Packages

| Package | Registry | What it is |
| --- | --- | --- |
| `foliant-protocol` | [PyPI](https://pypi.org/project/foliant-protocol/) | This reference implementation and Python SDK (`import foliant`) |
| `langchain-foliant` | [PyPI](https://pypi.org/project/langchain-foliant/) | LangChain tool, budget middleware and crew helper ([source](https://github.com/gazoy/langchain-foliant)); listed in [LangChain's integration directory](https://docs.langchain.com/oss/python/integrations/providers/all_providers) (langchain-ai/docs#6306, merged 30 Sep 2026) |
| `foliant-client` | [npm](https://www.npmjs.com/package/foliant-client) | TypeScript client, byte-compatible with the reference ([source](https://github.com/gazoy/foliant-js)) |
| `elizaos-plugin-foliant` | [npm](https://www.npmjs.com/package/elizaos-plugin-foliant) | ElizaOS plugin: `PAY_X402` action and `FOLIANT_BUDGET` provider ([source](https://github.com/gazoy/plugin-foliant)) |

## Layout

| Path | Whitepaper | What it holds |
| --- | --- | --- |
| `foliant/crypto.py` | §2 principle 9 | Ed25519 keys, canonical hashing, `Signed` messages with a `scheme` tag for signature agility |
| `foliant/accounts.py` | §6.1-6.3a | `AgentAccount` (with `parent`), `Policy` (with `within`), `Attestation`, `AgentSigner` (the enclave side that refuses to sign outside policy) |
| `foliant/channels.py` | §6.4, §6.6 | `Channel`: monotonic signed updates, settle, close with timeout, streams |
| `foliant/pools.py` | §6.10 | `Pool`: shared deposit, per-member claims, batch settlement, unilateral exit, contest |
| `foliant/market.py` | §6.7, §6.11 | `ServiceOffer`, `Receipt`, `Contribution` and royalty cascade |
| `foliant/ledger.py` | §5 (state only) | In-memory ledger: envelopes, nonces, policy enforcement up the account tree, delegation and recall, escrow accounting |
| `foliant/agent.py` | SDK | Agent-side helper: builds envelopes and off-chain updates |
| `foliant/x402.py` | §6.5, §6.9 | `PaymentGate` (provider middleware, FastAPI) and `AgentHttpClient` (pays on 402) |
| `demo/` | | A metered API, the four-scenario demo and six further use-case scenarios |
| `tests/` | §10 | Invariants: conservation, bounds, replay, stale/forged updates, unilateral exit, policy windows, the budget tree |
| `sim/` | §12 open problems | Cross-ledger budget tree study: specification, simulation, results, two independent audits |

## The rule that matters

The spending policy bounds **committed** value. A deposit into a channel or
pool is the spend that the policy sees, on both the signer (enclave) side and
the ledger side, using the same `Policy.check`. Off-chain updates are bounded
by the deposit they draw on, so they need no further budget check; the signer
still enforces allow/deny lists, expiry and monotonic balances on every update.
This is what makes "the enclave refuses to sign" and "the chain refuses to
apply" the same decision.

Accounts form a tree. A signer may **delegate** a child account with a policy
no wider than its own and fund it; value leaving the tree is checked against,
and recorded in, every ancestor's policy, all or nothing, so no branch can
commit more than any ancestor allows even after a parent is tightened. Moves
inside the tree are not spends. Any ancestor's signer can set a descendant's
policy, rotate its signer or recall its funds. That is how an orchestrator
gives its crew a budget and takes it away without a human.

## What is not here

Fees, the paymaster and the zero-fee lane (§5); consensus, data availability
and checkpoints (§4-5); IBC (§7); governance and token issuance (§8-9). Those
are borrowed components and are specified in the whitepaper, not prototyped.

## Invariants tested

- Channel: payee receives exactly the highest signed balance; payer gets back the rest; escrow ends at zero; stale, forged and tampered updates are rejected; a close finalises after the timeout regardless of the payee; a payee beats a dishonest close with a newer update.
- Pool: coordinator receives exactly the sum of highest signed balances; every member can exit unilaterally with or without the coordinator; a stale exit is contested; a stale update in a batch is skipped, not fatal; supply is conserved.
- Policy: for any sequence of attempted spends, committed value in any window never exceeds `per_window_max`, and signer and ledger agree at every step; escalation co-signature lifts `per_tx_max`; nonces prevent replay; the owner rotates the signer and the old key is dead.
- Tree: a child policy wider than its parent is refused; over random trees and random spends, no subtree ever commits more than its root's cap and every ancestor's window equals its subtree's total; a tightened parent binds existing children at once; funding and recall are not spends; only the owner or an ancestor's signer administers a descendant.
- Attestation: only quotes from a trusted vendor, for the account's own signer key, are accepted; an offer can require a code hash.
- 402: terms are x402-shaped; replay and underpayment get a 402 with a reason; receipts are provider-signed; one `settle()` pays out many calls.

## Licence

Copyright 2026 Machine Quotient Ltd. Licensed under the [Apache License 2.0](LICENSE).
