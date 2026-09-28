# Foliant — reference implementation (Python)

Executable specification of the Foliant agent layer (whitepaper §6): agent
accounts with runtime-enforced spending policies, TEE-style attestation,
payment channels and streams, Ark-style pooled channels, HTTP 402 metering in
the x402 wire format, and the compute-and-data market objects.

This is a **spec plus demo**, not a chain. It runs in one process with an
in-memory ledger so the state machines and their invariants can be read,
changed and tested quickly. The Cosmos SDK port implements the same state
machines in Go; the tests here become its conformance suite.

## Name

The project was called Concord until 28 September 2026; the name was changed to Foliant after a UK trade-mark search. Older links, issues and messages that say Concord refer to this project.

## Documents

- [Whitepaper v0.1](docs/whitepaper.md) — the full design (§6 is what this repo implements)
- [Design, feasibility and cost study](docs/study.md) — chain survey, build routes, costs, risks

## Demo

![Foliant demo](demo/video/foliant-demo.gif)

[Two-minute narrated walkthrough (MP4)](demo/video/foliant-demo-voiced.mp4) of the four scenarios, by the author.

`python demo/make_video.py` regenerates the captioned video, GIF and voiceover script in `demo/video/`; pass the voiceover's section boundaries in seconds to re-time the captions.

## Run

```bash
pip install -r requirements.txt
python demo/run_demo.py     # four scenarios, ~1 s
pytest                      # 22 tests incl. property-based invariants
```

## Layout

| Path | Whitepaper | What it holds |
| --- | --- | --- |
| `foliant/crypto.py` | §2 principle 9 | Ed25519 keys, canonical hashing, `Signed` messages with a `scheme` tag for signature agility |
| `foliant/accounts.py` | §6.1-6.3 | `AgentAccount`, `Policy`, `Attestation`, `AgentSigner` (the enclave side that refuses to sign outside policy) |
| `foliant/channels.py` | §6.4, §6.6 | `Channel`: monotonic signed updates, settle, close with timeout, streams |
| `foliant/pools.py` | §6.10 | `Pool`: shared deposit, per-member claims, batch settlement, unilateral exit, contest |
| `foliant/market.py` | §6.7, §6.11 | `ServiceOffer`, `Receipt`, `Contribution` and royalty cascade |
| `foliant/ledger.py` | §5 (state only) | In-memory ledger: envelopes, nonces, policy enforcement, escrow accounting |
| `foliant/agent.py` | SDK | Agent-side helper: builds envelopes and off-chain updates |
| `foliant/x402.py` | §6.5, §6.9 | `PaymentGate` (provider middleware, FastAPI) and `AgentHttpClient` (pays on 402) |
| `demo/` | | A metered API and the four-scenario demo |
| `tests/` | §10 | Invariants: conservation, bounds, replay, stale/forged updates, unilateral exit, policy windows |

## The rule that matters

The spending policy bounds **committed** value. A deposit into a channel or
pool is the spend that the policy sees, on both the signer (enclave) side and
the ledger side, using the same `Policy.check`. Off-chain updates are bounded
by the deposit they draw on, so they need no further budget check; the signer
still enforces allow/deny lists, expiry and monotonic balances on every update.
This is what makes "the enclave refuses to sign" and "the chain refuses to
apply" the same decision.

## What is not here

Fees, the paymaster and the zero-fee lane (§5); consensus, data availability
and checkpoints (§4-5); IBC (§7); governance and token issuance (§8-9). Those
are borrowed components and are specified in the whitepaper, not prototyped.

## Invariants tested

- Channel: payee receives exactly the highest signed balance; payer gets back the rest; escrow ends at zero; stale, forged and tampered updates are rejected; a close finalises after the timeout regardless of the payee; a payee beats a dishonest close with a newer update.
- Pool: coordinator receives exactly the sum of highest signed balances; every member can exit unilaterally with or without the coordinator; a stale exit is contested; a stale update in a batch is skipped, not fatal; supply is conserved.
- Policy: for any sequence of attempted spends, committed value in any window never exceeds `per_window_max`, and signer and ledger agree at every step; escalation co-signature lifts `per_tx_max`; nonces prevent replay; the owner rotates the signer and the old key is dead.
- Attestation: only quotes from a trusted vendor, for the account's own signer key, are accepted; an offer can require a code hash.
- 402: terms are x402-shaped; replay and underpayment get a 402 with a reason; receipts are provider-signed; one `settle()` pays out many calls.

## Licence

Copyright 2026 Machine Quotient Ltd. Licensed under the [Apache License 2.0](LICENSE).
