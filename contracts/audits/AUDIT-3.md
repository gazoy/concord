# AUDIT-3: `foliant/chain.py` — the EVM backend (ChainLedger, ChainAgent, ChainGate, ChainHttpClient), `demo/serve_chain.py`

Independent reviewer, 30 Sep 2026, commit e9768bd. Not one of the two contract auditors; the contracts
(`AgentAccounts.sol`, `PaymentChannels.sol`, `Pools.sol` at e9768bd, which is AUDIT-2 round 2 plus the
author's D-1/A2-9 resolution) are treated as ground truth, together with the reference gate
`foliant/x402.py` and AUDIT-2 "Verification, round 2". `foliant/` and `contracts/src/` are untouched.

## Scope and method

- Read `foliant/chain.py` in full, `demo/serve_chain.py`, `tests/test_chain.py` (run: 2 of 2 pass on Anvil),
  the three contracts, `x402.py`, and AUDIT-2 §"Verification, round 2".
- Probed `eth_account` 0.14.0 / `eth_keys` 0.8.0 / `web3` 7.16.0 / `eth_abi` recovery and encoding
  behaviour against OpenZeppelin `ECDSA.tryRecover` (the contracts' verifier).
- Compared the packaged ABIs/bytecode in `foliant/abi/*.json` with `contracts/out/*.sol/*.json` and the
  build metadata's source hashes with the current `src/`.
- Wrote `tests/test_chain_audit.py` (20 tests, Anvil): 12 fail and each failure is a finding below
  (`# FINDING A3-n`); 8 pass and are the verified-correct list (`# VERIFY A3-...`).
  `cd /home/claude/concord && python -m pytest -q tests/test_chain_audit.py`.

Severity is for a provider running the gate against real value; the testnet-demo impact is stated
separately in the verdict.

## Findings

### A3-1 High: the gate accepts signatures the contract rejects (high-s and v∈{0,1}), so a payer is served for an update that can never settle, and one such pool update blocks settlement for the whole pool

Lines: `chain.py` 169-171 (`_recover_typed`), 354, 365 (the only signature checks in `verify`).

`Account.recover_message` (eth_keys) accepts any `s < n` and any `v` in {0, 1, 27, 28}. OpenZeppelin
`ECDSA.tryRecover` (`PaymentChannels._verify`, `Pools._verify`) rejects `s > n/2` (`InvalidSignatureS`)
and, for a 65-byte signature, passes `v` to `ecrecover` unchanged, so `v` = 0/1 recovers `address(0)`
(`InvalidSignature`). Both recover to the correct signer off-chain. Confirmed empirically
(`tests/test_chain_audit.py::test_gate_accepts_high_s_signature_the_contract_rejects`,
`::test_gate_accepts_v01_signature_the_contract_rejects`).

Failure sequence (channel): payer signs a valid update, flips it to `(r, n-s, v')` (or `v-27`), sends
it. `verify` recovers the channel's signer → accepted, `revenue_unsettled += paid`, the call is served.
`settle()` → `PaymentChannels.settle` reverts `Unauthorized`; the provider has no settleable update for
that balance. Every subsequent update from the same payer, if honest, will settle the cumulative
balance, so the loss is bounded by what the payer chooses never to sign honestly again — i.e. the whole
channel's worth of calls. Failure sequence (pool): the same header on the pool scheme. `Pools.settle`
reverts the batch on a bad signature, so the entry in `latest` blocks settlement of every member until
the provider hand-edits `latest` (`::test_high_s_pool_update_bricks_the_whole_pool_settlement`). Combined
with A3-5/A3-6, `settle()` raises at that entry forever.

Fix: in `_recover_typed`, before recovery, require `len(sig) == 65`, `27 <= v <= 28` and
`s <= 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0`; refuse otherwise. This makes
the gate's acceptance set equal to the contract's. (Alternatively, verify via `eth_call` to a view that
runs `ECDSA.tryRecover` on-chain, but the local check is cheaper and exact.)

### A3-2 High: the gate's memory key is the raw header string, so every accepted-but-unsettled update replays under a differently-spelled id

Lines: `chain.py` 348 (`bytes.fromhex(u["id"][2:])` — any 2-char prefix, any hex case), 372
(`key = f"{scheme}:{u['id']}:{u['account']}"`), 373-376.

The chain lookups use the decoded bytes, so `"0xAB.."`, `"0xab.."`, `"0Xab.."`, `"zzab.."` all name the
same channel/pool and pass every chain check and the signature check. The staleness baseline is
`self.latest.get(key)`, keyed by the *string*, so a respelled id has no `prev` and is measured against
on-chain `paid`. The same signed update (no new signature needed) is then accepted again, once per
spelling: a 32-byte id has ~24 hex letters on average, so ~2^24 spellings; each unsettled balance is a
free call per spelling. For the channel scheme `ch["payer"] != u["account"]` (line 352) is a string
compare against the lowercase `_hex`, so only the id can be respelled; for the pool scheme both id and
account can (`::test_latest_key_is_not_canonical_so_updates_replay`,
`::test_latest_key_not_canonical_pool_variant`).

Failure sequence: payer sends balance 3 (accepted), balance 6 (accepted); re-sends balance 3 with the id
upper-cased → accepted, `paid = 3`; re-sends balance 6 upper-cased → accepted. Four calls for 6 units;
repeat with the next spelling. The provider's `settle()` then sends one tx per key (line 420-429): the
first settles 6, the rest are skipped (`balance <= paid`) — no on-chain loss beyond the free calls, but
the calls are free.

Fix: canonicalise before anything else: require `id`/`account` to be `0x` + 64 lowercase hex (or decode
and re-encode with `_hex`), and build the key from the *decoded* values:
`key = f"{scheme}:{_hex(obj_id)}:{_hex(acct)}"`. Do the same for the channel payer comparison (compare
bytes, `c[0] == acct`). The reference gate has no such issue only because its ids come from the ledger's
own canonical strings.

### A3-3 Medium: `latest` is process memory; a restart (or a second worker) replays every unsettled update

Lines: `chain.py` 329 (`self.latest: dict`), 374 (`last_bal = ... onchain_bal`), `serve_chain.py` 35, 76.

After a restart the baseline for every channel/claim is on-chain `paid`. Every update accepted before
the restart and not yet settled is accepted again (`::test_gate_restart_replays_unsettled_updates`: 3 of
3 replay). A payer who keeps its updates gets `unsettled / price` free calls; a payer who then continues
honestly makes the provider whole (its next balance is cumulative), and a payer who stops leaves the
provider with nothing to settle. So the loss is exactly the revenue unsettled at the moment of restart,
against a payer who replays and stops. Two uvicorn workers behave as two restarts that never settle.
On-chain `paid` protects the provider from *double* settlement, not from double service.

Fix (any of, in order of cost): settle on shutdown (`atexit`/lifespan hook calling `gate.settle()`) and
never run more than one worker; persist `latest` (a JSON file keyed as above, written on every accept,
loaded on start); or settle on a timer so the exposure is one interval. Document that `latest` is the
provider's only record of unsettled revenue.

### A3-4 Low: malformed headers produce 500s, not 402s; the integer type check runs after recovery

Lines: `chain.py` 354/365 (recovery) before 370 (type check); 385 (`except (KeyError, ValueError,
TypeError, FoliantError)`).

`eth_abi.exceptions.EncodingError` (float, bool, negative, `> 2^64` seq, `> 2^256` balance),
`eth_utils.exceptions.ValidationError` (64/66-byte sig), `IndexError` (empty sig) and
`web3.exceptions.ContractCustomError` (`get()` reverts `NoChannel` for an unknown id) all inherit from
`Exception` directly, so they escape the dependency and FastAPI returns 500 with a traceback in the log
(`::test_malformed_headers_return_402_not_500`: 8 of 8 cases are 500). Nothing is accepted wrongly:
`bool` (an `int` subclass, so it passes line 370) is refused by eth_abi; a string balance is refused at
370 after a wasted recovery. The reference gate had the same shape but its `verify_update` raised
`FoliantError`.

Fix: move the type/range check (`int` and not `bool`, `0 <= seq < 2^64`, `0 <= balance < 2^256`,
sig is 65 bytes) before recovery; add `eth_abi.exceptions.EncodingError`,
`eth_utils.exceptions.ValidationError`, `IndexError` and `web3.exceptions.Web3Exception` to the except
tuple (or wrap the chain reads and recovery in `try`/`except Exception as e: raise FoliantError(...)`).

### A3-5 Medium: a closed channel left in `latest` makes `settle()` raise at that entry forever, so entries after it are never settled

Lines: `chain.py` 420-429 (no per-entry error handling, no pruning), 438 (`revenue_unsettled = 0` only
on full success).

If the payer closes with an empty or older update (`beginClose(id, 0, 0, "")`) and finalises after the
timeout before the provider settles, the channel is `closed` with `paid` below the gate's balance. The
value on that channel is lost by design (the provider had the timeout to act — but nothing in the gate
or server watches `Closing`, see I-4). The bug is the blast radius: `settle()` calls `_send`, which
raises (`ContractCustomError` from `estimateGas`, or `FoliantError` if it reached the chain), the loop
aborts, and every entry *after* it in insertion order — other channels and the whole pool batch — is
never settled; the entry is never removed so every later `settle()` (and every `POST /settle`, which
becomes a permanent 500) fails the same way (`::test_closed_channel_entry_blocks_all_later_settlement`).

Fix: per-entry `try`/`except`; skip and drop entries whose channel is `closed`; collect failures and
return them; build the pool batch first so a channel failure cannot starve it. Subtract settled amounts
from `revenue_unsettled` as they succeed rather than zeroing at the end.

### A3-6 Medium: a member that exits and rejoins before settlement leaves an epoch-N entry that reverts the whole pool batch

Lines: `chain.py` 430-437 (every pool entry goes into one `settle`), 363 (epoch is checked at accept
time only). Contract: `Pools.settle` line 128 (`_verify` before the stale check; a bad signature reverts
the batch, as documented).

Sequence: member pays (accepted, epoch 1); `beginExit` with an empty update; wait `timeoutSecs`;
`finalizeExit`; `join` again (epoch 2, `exited = false`). The gate's epoch-1 entry now fails `_verify`
(`Unauthorized`) and, since it is not `exited`, is not skipped: the batch reverts, and no member of the
pool can be settled while the entry remains (`::test_member_exit_and_rejoin_bricks_pool_settlement`).
Cost to the attacker: three transactions and a deposit it gets back; it needs no signing trick. With the
demo's 3600 s pool timeout the provider has an hour to settle and nothing does so automatically. The
gate's acceptance criteria (recovery + epoch + balance range) are right at accept time; they cannot be
right at settle time without a re-check.

Fix: in `settle()`, re-read `claimOf` for each pool entry and drop entries whose `epoch != u["epoch"]`,
whose claim is `exited`, or whose `balance <= paid`; on a batch revert, fall back to one `settle` per
member so one bad entry cannot hold the others (or bisect). Settle promptly when an `Exiting` event is
seen (I-4).

### A3-7 Low (Medium on mainnet): `settle()` sends a pool transaction even when nothing is new, and `POST /settle` is unauthenticated

Lines: `chain.py` 430-437 (pool entries are never compared with on-chain `paid` and never pruned);
`serve_chain.py` 67-70.

Channel entries are skipped when `balance <= paid`; pool entries are sent every time
(`::test_settle_sends_a_pool_tx_even_when_nothing_is_new`: second `settle()` → `total 0`, one tx). The
contract skips them (verifying each signature first, so gas grows with the number of historical
members), and the coordinator pays for a no-op. Anyone can `POST /settle`, so anyone can make the
provider pay that gas at will. Not harmful to correctness (anyone can settle on-chain anyway, with their
own gas), but here it is the provider's gas.

Fix: skip pool entries whose `balance <= claim.paid` and prune them; send nothing when the batch is
empty; authenticate `/settle` (a shared secret header, or bind it to localhost only) or drop the
endpoint in favour of a timer.

### A3-8 Low: a reverting transaction surfaces as `web3.exceptions.ContractCustomError`, not `FoliantError`

Lines: `chain.py` 190-199 (`_send`), 400-408. `build_transaction` runs `eth_estimateGas`, which reverts
first, so the `FoliantError("transaction reverted: <hash>")` path is only reached when state changes
between estimate and inclusion. The docstring, `foliant.errors` and the reference `Agent` promise
`FoliantError`/`PolicyViolation`; `tests/test_chain.py` line 83 hides this with `pytest.raises(Exception)`
(`::test_policy_revert_surfaces_as_web3_error_not_foliant_error`). The good part of this: a policy
violation costs no gas, so the "learn on revert" concern (item 5 of the brief) is not a cost issue; it is
an accepted difference from the reference `AgentSigner`'s local refusal, provided the error type is fixed.

Fix: catch `web3.exceptions.ContractLogicError` (parent of `ContractCustomError`) in `_send`, decode the
custom error selector against the three ABIs (`PolicyViolation(string)` → `PolicyViolation`, others →
`FoliantError`) and re-raise.

### A3-9 Low: the agent advances `latest` before the provider accepts, so any refused request makes the next update pay double

Lines: `chain.py` 268, 285 (`self._seq[cid], self.latest[cid] = seq, u` unconditionally); 258, 275
(`prev` is the local latest). One dropped/refused request (network error, a 402 for A3-3/A3-4 reasons,
a server 500) and the next accepted update carries both amounts: the gate correctly measures `paid`
from its own last accepted balance, so the provider is paid for a call it never served
(`::test_agent_advances_latest_on_rejected_payment_and_overpays`: `paid == 6` for one call). The
reference agent has the same structure; on-chain this is the agent's own money, so Low, but a
`ChainHttpClient` retry loop would compound it.

Fix: `ChainHttpClient.request` should roll back `agent.latest[cid]`/`_seq` to the pre-call values when
the paid request does not return 200 (or `pay_*` should return a candidate that is committed only by
the caller on success).

### Informational

- I-1 `ChainAgent._send` / `ChainGate._send` take the nonce from `get_transaction_count(address)`
  (block `latest`), so two concurrent sends from one key collide (`nonce too low` / replacement
  underpriced). Single-threaded use only, or use `"pending"` plus a lock.
- I-2 `ChainHttpClient`: (a) the channel-reuse loop (lines 475-482) makes one `get()` RPC per known
  channel per request; cache the channel struct (deposit/payee/token never change; only `closing_at`/
  `closed` do). (b) `salt=agent.L.now` (line 485): two opens in one block timestamp → `ChannelExists`
  → the request raises. Avalanche can produce consecutive blocks with equal timestamps; use a counter
  or random salt. (c) Only channels present in `agent.latest` are reused, so an agent restart opens a
  new channel each time (and the old one's remainder sits until closed). (d) `default_deposit < price`
  joins/opens (a policy spend, gas) and then `pay_*` raises `PolicyViolation`; check first.
  (e) Receipts are appended unverified; verification procedure is below.
- I-3 `serve_chain.py`: with no `FOLIANT_POOL_ID`, `create_pool(salt=FOLIANT_POOL_SALT or 0)` on every
  start; the second start with the same key and salt reverts `PoolExists` during `estimateGas` and the
  server fails to start — so restarts do *not* silently orphan claims, they crash unless
  `FOLIANT_POOL_ID` (recommended) or a new salt is set. With a new salt, members' claims in the old pool
  are orphaned: they can still exit unilaterally (deposit returns after the timeout, at their gas), and
  the provider can still settle the old pool only by hand. When `FOLIANT_POOL_ID` is set, the gate does
  not check `pool.coordinator == provider` (the reference does, `x402.py` line 112): a misconfigured id
  makes every settlement pay someone else's coordinator. Add the check to `verify` or at start-up.
  `GET /chain` exposes only public data; `/health` exposes `revenue_unsettled`, harmless.
- I-4 Nothing watches `Closing`/`Exiting` events or settles on a timer; the provider's revenue on a
  closing channel or exiting claim depends on someone calling `POST /settle` inside the timeout. For
  the demo this is the operator; for anything else it must be a background task (and see A3-5/A3-6 for
  why a missed one is worse than a lost channel).
- I-5 `verify` mutates `latest`/`revenue_unsettled` before the handler runs (line 382-383); a handler
  500 consumes the payment. Same as the reference; document or move the commit to the receipt step.
- I-6 `offer.id` (line 303) does not include `unit` or `descriptor`; harmless for the demo but two
  offers differing only in unit share an id.
- I-7 The gate refuses updates on a channel with `closing_at` set (line 351), which the reference does
  not (`x402.py` 107). This is stricter and correct: the provider should settle, not accept more.

## Receipt verification (item 3)

`receipt()` (lines 393-398) signs `json.dumps(body, sort_keys=True)` with EIP-191 (`personal_sign`
prefix) where `body = {updateId, requestHash, responseHash, offerId}` and
`updateId = keccak(json.dumps(update, sort_keys=True))` over the update dict *as the client sent it*
(including `sig` and any extra keys). `sort_keys` makes it order-independent; the `sig` inclusion is
correct (it binds the exact signed object). Two caveats: the hash is over the client's spelling of
`id`/`account` (see A3-2 — canonicalising fixes this too), and Python's default separators
`(", ", ": ")` and `ensure_ascii=True` are part of the format, so a non-Python client must reproduce
them exactly. A client verifies with:

```python
rec = json.loads(base64.b64decode(header))
body = rec["body"]
assert body["updateId"] == Web3.keccak(text=json.dumps(update_sent, sort_keys=True)).hex()
assert body["requestHash"] == Web3.keccak(request_bytes).hex()
assert body["responseHash"] == Web3.keccak(response_bytes).hex()
assert body["offerId"] == offer_id_from_402_terms
signer = Account.recover_message(encode_defunct(text=json.dumps(body, sort_keys=True)), signature=rec["signature"])
assert signer == rec["signer"]["key"] == pay_to_from_402_terms
```
(`::test_receipt_verifiable_by_client`.) Note the receipt does not state the amount accepted; the
client learns it from its own update.

## Checked and correct

- EIP-712 digests: `encode_typed_data(domain, types, msg)` hashes to exactly
  `PaymentChannels.updateDigest` / `Pools.updateDigest` for ids/accounts with leading zero bytes,
  `seq = 2^64-1`, `balance = 2^256-1`, and zeros (`::test_channel_digest_matches_contract`,
  `::test_pool_digest_matches_contract`). Domain names/version/chainId/verifyingContract and the two
  type strings match the contracts' `UPDATE_TYPEHASH`.
- ABI packaging: `foliant/abi/{AgentAccounts,PaymentChannels,Pools,MockERC20}.json` `abi` and
  `bytecode` are byte-identical to `contracts/out/*.sol/*.json` (solc 0.8.30, optimizer 200), and the
  build metadata's source keccaks equal the current `src/*.sol`, i.e. the audited round-2 sources.
  `deploy_local` deploys accounts → channels(accounts) → pools(accounts) → `lockModules([channels,
  pools])` → token, matching `Deploy.s.sol`'s order.
- Staleness by balance (not seq), `balance <= deposit`, `paid >= price`: lines 375-381, matching the
  contracts and the reference after A2-1. Epoch is checked against the claim (363) and is in the
  signed message, so a stale-epoch update also fails recovery. Updates are verified against the
  snapshot `Channel.signer` / `Claim.signer` (354, 365), not the current signer — matches A2-4/D-1.
- Payee, token, `closed`, `closing_at`, payer-account binding (351-352), pool id (358), active member
  and `exit_at` (361-362) are all refused with 402 (`::test_gate_rejections_that_are_correct`).
- Scheme/key separation: the key carries the scheme string and `settle` splits on it; a channel id and
  a pool id can never collide (different preimage tags), so the reference's `kind` vs the scheme name
  is a naming difference only.
- `settle()`: a channel update superseded on-chain is skipped without a tx, and the gate's next
  baseline is its own memory (`::test_channel_superseded_on_chain_is_skipped`); a revert mid-way does
  *not* zero `revenue_unsettled` (`::test_settle_revert_midway_keeps_revenue_unsettled_accounting`),
  though it also does not subtract what did settle. Pool `total` is measured from the token balance
  delta, so it is right even when the contract skips entries.
- `ChainAgent.pay_channel`/`pay_pool` baseline: local `latest` if present, else on-chain `paid`;
  since only the agent signs, `latest >= paid` always holds and a provider settle in between neither
  under- nor over-pays. After an agent restart its balance restarts from `paid` and is stale for the
  gate until the provider settles (`::test_agent_restart_before_provider_settles_is_stuck`) — a
  liveness nuisance, not a loss. `join_pool`/`finalize_exit` clear `latest`/`_seq` so the new epoch
  starts at seq 1; `close_channel`/`begin_exit` pass the latest update (or empty), consistent with the
  snapshot-signer rule for an un-rotated key.
- Integer edge cases (`bool`, float, negative, `> 2^64` seq, `> 2^256` balance, string) are all
  refused — but as 500s (A3-4). Nothing out of range is accepted.

## Verdict

**Fuji demo and testnet cost report: fit to run, with two small changes first.** A3-1 and A3-2 are each a
few lines (canonical signature check in `_recover_typed`; canonical key and byte comparison in `verify`)
and make the gate's acceptance set equal to the contracts'. With scripted, honest agents the demo and
its gas figures are unaffected by any finding here; the cost report's numbers (one tx per join/open, one
per channel settle, one per pool batch) are what the code does. Run one worker, settle before stopping
the server (A3-3), and set `FOLIANT_POOL_ID` on restarts (I-3).

**Before any mainnet use:** fix A3-1 through A3-7 (A3-5/A3-6 together: per-entry settlement with
re-checks and pruning, and a settle-on-`Closing`/`Exiting`/timer task), persist `latest` (A3-3),
authenticate or remove `/settle` (A3-7), fix the error contract (A3-8) and the client-side rollback
(A3-9), add the coordinator check when a pool id is supplied (I-3), and re-run `tests/test_chain_audit.py`
expecting 20 of 20. The contract-level items carried from AUDIT-2 (A2-7 settle deadline, malicious-payer
fuzz, A2-6/A2-8 docs) still stand and are outside this file's scope.

## Resolution (author, 30 Sep 2026)

| Finding | Action |
| --- | --- |
| A3-1 High | Fixed. `_check_sig`: 65 bytes, v ∈ {27, 28}, low-s, applied before any recovery and in `verify`. |
| A3-2 High | Fixed. `_canon32` parses ids strictly (0x + 64 hex) and `verify` rewrites the update to canonical lower-case hex before keying; payer compared case-insensitively. |
| A3-3 Medium | Fixed. `ChainGate` persists `latest` and `revenue_unsettled` to a state file (per chain, provider and offer; `FOLIANT_STATE_DIR` or cwd; `state_dir=None` for memory-only) after every accept and settle; `serve_chain.py` settles on shutdown. Single worker remains the documented deployment. |
| A3-4 Low | Fixed. Types, ranges and signature form are validated before recovery; any exception in `verify` becomes a 402 with the terms. |
| A3-5 / A3-6 / A3-7 | Fixed. `settle()` re-checks every entry against chain state (closed channel, exited or re-epoched member, balance already reached) and prunes; a failing channel entry is kept for retry without blocking others; a reverting pool batch falls back to one transaction per member and unsettleable entries are dropped; no transaction is sent when nothing is new. `serve_chain.py`: `POST /settle` can require `X-Settle-Key` (`FOLIANT_SETTLE_KEY`); `FOLIANT_POOL_ID` must be coordinated by the provider key. |
| A3-8 Low | Fixed. Reverts at estimation are decoded from the contracts' error ABIs into `PolicyViolation` / `InsufficientFunds` / `FoliantError`; no gas is spent. |
| A3-9 Low | Fixed. `pay_channel`/`pay_pool` produce a *pending* update; `confirm(id)` after a 200 makes it the agent's latest, `discard(id)` otherwise (the http client does both); re-signing without confirming re-signs at the same balance. Channel salts are random 64-bit. |
| I-1 | Nonces are taken from the pending block. |

`tests/test_chain_audit.py` now asserts the fixed behaviour (one reproduction is skipped as moot); 58 Python tests pass. Verification by the auditor follows.
