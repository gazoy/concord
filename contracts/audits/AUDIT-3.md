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

## Verification, round 2 (auditor, 30 Sep 2026, commit 6bcca8c)

Method: re-read the full `foliant/chain.py` and `demo/serve_chain.py` at 6bcca8c and the author's changes
to the tests; probed `_check_sig` against OpenZeppelin 5.4 `ECDSA.tryRecover` on the boundaries; attacked
the four new mechanisms (state file, per-entry settlement, pending/confirm, revert decoding) by hand and
with 13 new tests appended to `tests/test_chain_audit.py` (`# VERIFY-2` / `# FINDING A3-10..15`).
`tests/test_chain.py`: 2 of 2. `tests/test_chain_audit.py`: 28 pass, 1 skipped (the A3-1 pool-brick
reproduction, moot), 6 fail = the six new findings below. `foliant/` and `contracts/src/` untouched.

### Status of round-1 findings

| Finding | Status | Evidence |
| --- | --- | --- |
| A3-1 High, non-canonical signatures | **Closed** | `_check_sig` (52-61) runs before recovery and again in `verify` (444). Acceptance set equals OZ 5.4 `tryRecover(bytes32,bytes)`: 65 bytes only (OZ 5 dropped the 64-byte EIP-2098 path; the gate requires 132 hex chars, so both reject compact signatures); `v ∈ {27,28}`; `0 < s ≤ n//2` — the boundary `s == n//2` (`0x7FFF…20A0`) is accepted by both, `n//2 + 1` rejected by both; `s == 0` rejected explicitly; `r == 0` or `r ≥ n` are not checked by the gate but eth_keys raises `BadSignature` on them, which `verify` turns into a 402, matching OZ's `address(0)` → `InvalidSignature`. (`test_v2_check_sig_matches_openzeppelin_set`; the original A3-1 tests now pass.) |
| A3-2 High, non-canonical keys | **Closed** | `_canon32` (39-43) requires `0x` + 64 hex; upper-case hex is accepted as input and rewritten to lower-case (445) before keying and the payer comparison; `0X`, missing prefix, wrong length, non-hex are refused. Replay under any spelling is refused (`test_v2_canonical_ids_and_replay_closed`). Note the receipt's `updateId` is now over the canonical form, so a client that sent upper-case ids must canonicalise before recomputing it. |
| A3-3 Medium, restart replay | **Closed for a single process; see A3-10, A3-11, A3-15** | State is saved after every accept and settle by write-to-tmp + `os.replace`, loaded at construction; entries are pruned on settle so the file does not grow (`test_v2_state_survives_restart_and_is_pruned_on_settle`: after settle the file is `{"latest": {}, "revenue_unsettled": 0}`). A corrupt or partial file makes the constructor raise (`json.loads`), i.e. the server refuses to start rather than starting empty — the safe direction; recovery is manual (`test_v2_corrupt_state_file_fails_closed`). `serve_chain.py` settles in the lifespan shutdown hook. |
| A3-4 Low, 500 on malformed headers | **Closed** | Types, ranges and signature form are checked at 441-444 before any RPC or recovery; `except Exception` at 483. All eight malformed cases are 402. One residual: the 402 body carries `str(e)` verbatim, which for an OS error includes a filesystem path (see A3-15). |
| A3-5 Medium, closed channel blocks settle | **Closed** | Per-entry: closed or already-reached entries are pruned (536-538); a failing tx keeps the entry and continues (541-542). Verified with the front-run case: a payer self-settling a higher update between the pre-check and the tx makes the tx revert `StaleUpdate`; the entry is kept, nothing is counted in `total`, and the next call prunes it (`test_v2_front_run_self_settle_keeps_accounting_right`). |
| A3-6 Medium, rejoin bricks pool batch | **Closed** | Pre-check drops entries whose claim is gone, exited, re-epoched or already at the balance (549-551); a reverting batch falls back to per-member transactions and drops the unsettleable one (561-568). Exit-with-higher-update + finalize + rejoin before settlement: honest member settled, `total` equals the token delta (the attacker's balance arrived via `beginExit`, correctly not counted), attacker's entry pruned, nothing retried forever. Can an attacker cause an honest entry to be dropped? Only a per-member tx failure drops an entry, and an honest member's single-update `settle` can only revert on bad signature or balance > deposit, both impossible for a gate-accepted update; `StaleUpdate` is a skip, not a revert, in `Pools.settle`. Forcing the fallback costs the attacker an exit cycle per attempt and costs the provider N transactions instead of one — bounded gas griefing, acceptable. |
| A3-7 Low, no-op pool tx and open /settle | **Closed** | Pool entries at or below `paid` are pruned and no transaction is sent for an empty batch; `X-Settle-Key` optional auth. Default remains open; set `FOLIANT_SETTLE_KEY` on anything public. |
| A3-8 Low, error type | **Closed** | `_decode_revert` maps `PolicyViolation(string)` (with the reason), `InsufficientFunds`, other custom errors → `FoliantError` (`test_v2_decoded_reverts`); `test_chain.py` now asserts `PolicyViolation`. |
| A3-9 Low, agent overpays after a refused call | **Closed; see A3-14 and the recovery note** | `pending`/`confirm`/`discard`; re-signing without confirming re-signs at the same balance (`test_v2_pending_confirm_and_dropped_response`). |
| I-1 nonce | Improved | `"pending"` block; still no lock, so truly concurrent sends from one key can collide. Single-threaded agent use only. |
| I-3 coordinator check | Closed | `serve_chain.py` 38-39 exits if `FOLIANT_POOL_ID` is not coordinated by `PROVIDER_KEY`. |

### New findings

**A3-10 Medium (config-dependent): two processes on one state file each accept every update once, and the last saver's memory overwrites the other's entries on disk.** Lines 413-416 (loaded once), 418-423 (whole-file replace). `uvicorn --workers 2`, or an old and a new process overlapping during a restart, or two services for one provider key/offer on one host: worker A accepts u1 and u2, worker B (loaded before) accepts u1 again and saves `{u1}` — u2 is gone from disk, and a later restart replays it too (`test_v2_two_processes_on_one_state_file_replay`). This is not a window, it is the whole session: each worker serves each update once. Fix: take an exclusive `fcntl.flock` on the state file around read-modify-write in `verify` and `settle` (re-load `latest` inside the lock before the staleness check, save, release), or refuse to start when the file is locked by another process. Until then, "single worker" is a hard requirement, not a recommendation; enforce it in `serve_chain.py` by never passing `workers`.

**A3-11 Low: the state file name embeds `offer.id`, so a restart with a different `FOLIANT_PRICE` (or token) starts from an empty state; every unsettled update replays once and the old file's entries are never settled.** Line 412 (`self.offer.id[2:10]` in the name), `ChainOffer.id` (378-380) includes the price. `test_v2_price_change_orphans_state_and_replays`. Fix: name the file by chain id + provider (+ token) only — the price is not part of what makes an accepted update unsettled — and keep the offer id inside the file for information; on load, entries for another offer are still valid updates to settle.

**A3-12 Low: pool entries are settled against `self.offer.pool_id`, not the pool in their key, and `serve_chain.py` loads the state before the pool id is known.** Lines 557, 564 (`self.offer.pool_id`) vs 548 (pre-check against `obj_id` from the key); `serve_chain.py` 35 vs 37-43; `ChainOffer.id` at construction has `pool=None`, so the same file loads whatever pool is chosen afterwards. After a crash (the shutdown settle did not run) and a restart with a new `FOLIANT_POOL_SALT`/`FOLIANT_POOL_ID`, the old pool's entries pass the pre-check on the old pool, are sent to the new pool, skipped by the contract as non-members, and popped as "settled": revenue abandoned without a trace (`test_v2_pool_entries_settle_against_offer_pool_not_their_own`: old claim `paid` stays 0, `latest` empty, `total` 0). Fix: group the batch by the key's pool id and call `settle(obj_id, …)` per pool; nothing about `settle` needs `offer.pool_id`.

**A3-13 Medium: a transient RPC error while re-checking a channel entry deletes it.** Lines 531-535: `except Exception: self.latest.pop(key)`. Any `ConnectionError`, timeout or node 5xx during `L.channel()` discards accepted, unsettled revenue permanently (`test_v2_transient_rpc_error_drops_channel_entry`). The intent was `NoChannel`; that revert cannot happen for a gate-accepted entry anyway (`verify` read the channel), so the clause should simply not catch. Fix: let the exception propagate (state is saved only on success, so nothing is lost), or catch only `ContractCustomError` with the `NoChannel` selector.

**A3-14 Low: `ChainHttpClient` opens a new channel for every refused first call.** Lines 609-619: reuse scans `agent.latest`, which after A3-9 holds only *confirmed* updates; a fresh channel whose first payment is refused (any 402/5xx, a network error) is never in `latest`, so the next request opens another channel — a deposit commit and a policy-window spend each time (`test_v2_http_client_opens_a_channel_per_failed_first_call`: 3 refusals → 3 channels). The round-1 code had the same scan but `latest` was written unconditionally, which hid it. Fix: keep a separate `agent.channels: set[bytes]` (populated by `open_channel`) and scan that, taking the baseline balance from `latest` if present else on-chain `paid`.

**A3-15 Medium: concurrent accepts corrupt each other's save and consume payments without serving them.** Lines 418-423 (one shared `.tmp` path), 477-479 (`latest`/`revenue_unsettled` mutated before `_save`), 483-484 (the resulting `FileNotFoundError` becomes a 402 whose body is `"[Errno 2] No such file or directory: '<state path>.tmp' -> '<state path>.json'"`). The dependency is a sync function, so FastAPI runs `verify` on a threadpool; with two accepts in flight, thread A truncates and writes `.tmp`, thread B truncates the same inode, A renames it into place, B writes into the (now live) state file and its own `os.replace` fails. Observed 2-4 failures in 8 concurrent valid requests (`test_v2_concurrent_accepts_consume_payments_without_serving`: all 8 in `latest`, `revenue_unsettled == 24`, only 4-6 served). The client discards its pending update, re-signs at the same balance, and is told "stale" (the stuck case in the note below); the provider keeps the money. The state file itself survives (the winner's content is coherent), but the 402 leaks a server path. The same missing lock also leaves the `prev` read (468) and the write (477) unguarded, a much narrower double-accept window under the GIL that the fix below closes too. Fix: a `threading.Lock` held from the `prev` read through `_save`; a per-writer tmp name (`f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"`); mutate `latest` only after the save succeeds (or roll back on failure); do not echo `OSError` text to clients.

### pending/confirm: the dropped-response case (item 3)

When the gate accepts and the client never sees the 200 (connection drop after commit, or a handler 5xx — the gate commits before the handler runs, I-5), the client discards its pending update and re-signs at the same balance; the gate answers `stale update`; nothing in `ChainHttpClient` retries, so the caller sees a 402 until the provider settles (the agent restarts from on-chain `paid` only when it has no `latest`, and here it does). Acceptable for the demo; not for a client that must make progress. Recommended recovery, safe by construction: a `stale update` 402 can only mean the gate holds a balance ≥ the client's last *signed* one, and the client never signs above its pending update, so the gate's latest is exactly the discarded pending. `ChainHttpClient.request` should, on a 402 whose `error` is `stale update` immediately after a discard, `confirm` the discarded update instead and pay again, once. Cost: one call's price per incident, bounded, never more than the client itself signed; a malicious gate gains nothing it did not already hold. Optionally the gate can put `lastAccepted: {id, account, balance}` in the stale 402 so the client can assert it equals its discarded update before confirming. (`test_v2_pending_confirm_and_dropped_response` demonstrates both the stuck state and this recovery by hand.)

### Verdict

A3-1 through A3-9 are closed as described, with the signature acceptance set proven equal to the contracts'
on the boundaries, the canonical keys replay-proof under every spelling tried, and the per-entry settlement
surviving the exit/rejoin, closed-channel and front-run sequences with correct `total` accounting.

The six new findings are all in the two new mechanisms and are all small: A3-13 is a one-line `except`
to delete; A3-12 is grouping the batch by pool; A3-15 and A3-10 are one lock (in-process and file) around
accept-and-save plus a unique tmp name; A3-11 is the file name; A3-14 is a set of opened channels.

**Fuji demo and cost report: fit to run at 6bcca8c** with these operating constraints: one worker, one
process per provider key and state directory, `FOLIANT_POOL_ID` fixed across restarts (or the same
salt), the same `FOLIANT_PRICE` across restarts, and the demo's scripted agents sending requests
sequentially (A3-15 needs concurrent paid requests; A3-13 needs an RPC failure exactly during settle —
if `POST /settle` raises, call it again before trusting the numbers). None of the six changes the
transaction counts or gas in the cost report. Fix A3-13 and A3-15 first if the demo drives concurrent
clients.

**Before mainnet:** A3-10, A3-13, A3-15 (locking and no-drop on error), A3-12, A3-11, A3-14, the
stale-update recovery in the client, and the items carried from round 1 (I-4 event/timer settlement is
still absent: the provider's revenue on a closing channel or exiting claim still depends on someone
calling `/settle` inside the timeout; I-5). Re-run `tests/test_chain_audit.py` expecting 34 of 34 with
the one skip, plus the AUDIT-2 contract carry-overs.

### Resolution of round-2 findings (author)

| Finding | Action |
| --- | --- |
| A3-10 Medium | Fixed. A cross-process `flock` on `<state>.lock` plus a process `threading.Lock` wrap each accept and each settle; the state file is reloaded inside the lock before the staleness check. |
| A3-11 Low | Fixed. State file named by chain, provider and token only. |
| A3-12 Low | Fixed. Pool entries are batched by the pool id in their key, not the offer's current pool. |
| A3-13 Medium | Fixed. No broad `except` around chain reads in `settle`; a transient RPC error propagates and nothing is dropped. |
| A3-14 Low | Fixed. The http client tracks the channels it opened and reuses them whether or not the first payment was confirmed. |
| A3-15 Medium | Fixed. Locking as above; per-writer temporary file (`mkstemp`) with atomic replace; state is mutated only after a successful save, and a save failure returns a generic 402 ("provider state unavailable, retry") without echoing the error. |
| Stale-update recovery | Implemented as proposed: on a "stale update" 402 immediately after a discard, the client confirms the discarded update (the only one the gate can hold) and pays once more. |

`tests/test_chain_audit.py`: 34 pass, 1 skipped (moot reproduction). Constraints that remain documented for the demo: one provider process per key and state directory is still the recommended deployment (the locks make more than one safe, but not faster); `FOLIANT_POOL_ID` should be fixed across restarts.

## Verification, round 3 (auditor, 30 Sep 2026, commit 879e6be)

Method: re-read the diff and the locked `verify`/`settle` paths, `_save`, `_FileLock`, `ChainAgent.confirm`/
`discard`/`discarded` and `ChainHttpClient.request`/`_pay_once`; hammered `verify` from 8 threads (distinct
and identical updates) and from two forked processes sharing one state directory; forced a failing save;
quantified the stale-update recovery against a lying gate. Four tests appended (`# VERIFY-3`).
`tests/test_chain.py` 2 of 2; `tests/test_chain_audit.py` 36 pass, 1 skipped (moot A3-1 repro), 0 fail.
`foliant/` untouched.

### Status of round-2 findings

| Finding | Status | Evidence |
| --- | --- | --- |
| A3-10 Medium, two processes | **Closed** | `threading.Lock` + `flock` on `<state>.lock` around `verify` and `settle`, `_load()` inside the lock before the staleness check. Two forked processes each fed all six updates (opposite orders, started together): 6 accepts in total, a fresh gate loads all 6 entries / 18 unsettled and refuses every replay (`test_v3_two_processes_share_state_exactly_once`). |
| A3-11 Low, state name | **Closed** | Named by chain, provider, token; the price is no longer in the name. |
| A3-12 Low, pool id | **Closed** | Batches keyed by the entry's pool id; `settle(pid, …)` per pool. |
| A3-13 Medium, drop on RPC error | **Closed** | No `except` around `L.channel`; an error propagates before `_save`, so the disk keeps the entry and the next `_load` restores memory. Idempotent: entries settled before the error are pruned on the next call by `balance <= paid`. |
| A3-14 Low, channel per failure | **Closed** | `ChainHttpClient.channels` records every open; reuse scans it with the baseline from `latest` or on-chain `paid`. |
| A3-15 Medium, concurrent saves | **Closed** | 8 threads, 8 distinct valid updates: 8 × 200, `latest` 8, 24 unsettled; 8 threads, one update: exactly one 200, seven 402, 27 unsettled on disk and in memory; `mkstemp` leaves no litter (`test_v3_threads_distinct_and_same_update_exactly_once`). Failed save (`OSError` from `_save`): memory rolled back to the pre-accept state, file byte-identical, generic 402 `provider state unavailable, retry` with no path, and the same header succeeds once the disk is back (`test_v3_failed_save_leaves_memory_and_file_consistent`). |
| Stale recovery | **Implemented; bound quantified below** | `discarded` fallback in `confirm`; the client confirms and pays once more on a `stale update` 402. Dropped-response case recovers at exactly one price (`test_v3_stale_recovery_bounds` (a)). |

### Stale recovery: bounds (item 3)

- Free call for the client: none. The gate serves only a balance strictly above its latest and only for
  a signature over that balance; confirming a discarded update moves the client's own baseline up, never the
  gate's down. Edited balances fail recovery; replays are stale (`test_v3_stale_recovery_bounds` (c)).
- Double charge by a malicious gate: **one extra price per incident, incidents unbounded.** A gate that
  answers `stale update` to every first paid attempt and serves the second collects 2 × price per served
  call (three calls: balance 9 → 27, `test_v3_stale_recovery_bounds` (b)). This is the designed cost
  (the client never signs more than pending + price per recovery), and the gate could already take the
  first signed update without serving, so the recovery does not hand it anything it did not hold; but it
  turns "take once and the client walks away" into "charge double for as long as the client stays".
  Recommendation (A3-16 Low, client side): rate-limit recoveries — allow one per object until the next
  successful call without recovery, or refuse after two consecutive incidents on the same object — and
  surface the incident to the caller (a receipt count vs. balance check makes it visible).

### New findings

**A3-16 Low: unbounded stale-update recoveries let a dishonest gate charge double indefinitely.** See above;
`chain.py` `ChainHttpClient.request` recovers on every request. Fix: a per-object recovery budget as above.

Informational, round 3: (i) `_load()` runs inside `verify` but outside `_verify_locked`'s `try`, so a state
file corrupted while the server runs (only possible by hand: writes are `mkstemp` + `os.replace`) yields a
500 per request rather than a 402; fail-closed is the right direction, but the operator should see a clear
log line. (ii) `request()` calls `r.json()` on every 402; a non-JSON 402 from an intermediary raises in the
client. (iii) `settle` holds both locks across its transactions, so accepts wait for the settlement to be
mined (seconds on Fuji): expected, document it. (iv) Round-1 I-4 stands: no `Closing`/`Exiting` watcher
or timer; the shutdown hook and `/settle` are the only settlement triggers.

### Verdict

Every finding from rounds 1 and 2 (A3-1 through A3-15) is closed at 879e6be, with exactly-once acceptance
verified across threads and across processes, saves atomic and rolled back on failure, settlement
per-entry and per-pool, and the client's recovery bounded to one price per incident.

**Fuji demo and cost report: fit to run at 879e6be, with no operating caveats beyond keeping
`FOLIANT_POOL_ID` fixed across restarts and settling (shutdown hook or `/settle`) before the pool or channel
timeouts.** Multiple workers and concurrent clients are now safe; none of the changes alters the transaction
counts or gas in the cost report.

**Before mainnet (all small, none blocking the testnet work):** A3-16 (recovery budget in the client); I-4
(settle on `Closing`/`Exiting` events or a timer inside the timeouts — the one remaining way for a provider
to lose accepted revenue); a log line for a corrupt state file; `FOLIANT_SETTLE_KEY` set whenever `/settle`
is reachable; and the AUDIT-2 contract carry-overs (A2-7 settle deadline, malicious-payer fuzz, A2-6/A2-8
documentation).

### Resolution of round-3 findings (author)

A3-16: the client allows at most two consecutive stale-update recoveries before refusing to pay a provider again (`ChainHttpClient.incidents`, reset on a clean success); a non-JSON 402 is returned to the caller rather than raising. Carried to the mainnet list: I-4 (settle on Closing/Exiting events and on a timer), a log line for a corrupt state file, `FOLIANT_SETTLE_KEY` wherever `/settle` is reachable, and the AUDIT-2 contract carry-overs.
