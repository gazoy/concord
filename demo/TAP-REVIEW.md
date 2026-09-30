# Tap review (demo/tap.py, its wiring in demo/serve_chain.py, tests/test_tap.py)

Independent review, 2026-09-30, against web3 7.16.0 / eth-account 0.14.0 with anvil. `python3 -m pytest
tests/test_tap.py -q` passes (4 tests). Reproductions for every numbered finding are in the scratch test
`test_tap_review.py` (7 tests, all pass, i.e. every claimed defect reproduces); the relevant assertions are quoted
below.

**Verdict.** The per-address rule and the input validation are sound and the persisted `given` set does survive a
restart, but the tap is not safe to leave on a public Fuji server as written. Two things let a stranger spend the
wallet or stall the server at will: (a) both limits are only charged after *both* transactions succeed, so any
failure path — a recipient contract that rejects value, a mint that reverts, a receipt timeout — costs the tap gas
or 0.02 AVAX, leaves the address eligible and leaves the hourly counter untouched, so it can be repeated without
bound; and (b) `POST /tap` is an `async def` that calls the blocking `give()` inline, so every tap freezes the whole
uvicorn process (including `/infer` and `/health`) for two block times in the good case and for up to 240 s when a
transaction is stuck. The state file's `flock` is on the temporary file and protects nothing, but with one process
that only matters if a second instance ever shares the directory. Nothing leaks the key, and exposing the tap's
address and balance on `GET /chain` is harmless beyond one RPC call per request. The tests cover the happy paths
they name and nothing else.

## Findings

### T-1 — High: any failed give is free for the caller and charged to the tap, with no limit
- **Location:** `demo/tap.py` `give()` lines 81–95 (`_given.add` / `_recent.append` only after both sends);
  `_send()` line 102 (status 0 → `TapError(502)`).
- **Evidence:** Give to a contract with no `receive()` (e.g. the demo's own `AgentAccounts` address). The 21 000-gas
  value transfer is mined with status 0, the tap pays the gas, `give()` raises 502, and neither `_given` nor
  `_recent` is touched. Repro `test_reverting_recipient_burns_gas_without_limits`: three calls with `per_hour=1`
  all reach the chain, tap balance drops each time (93 386 734 329 000 wei on anvil), `status()["given"] == 0`
  and `len(tap._recent) == 0` afterwards. At Fuji prices this is roughly 0.0005 AVAX per call and, because of T-4,
  it also holds the server for a block per call, so the drain and a denial of service come together.
- **Fix:** Charge the hourly counter (and ideally a short per-address cooldown) *before* broadcasting, i.e. append
  to `_recent` as soon as the request passes the checks, not after success. Additionally refuse recipients with
  code (`w3.eth.get_code(to)`), since a contract cannot be "a visitor trying the protocol" and is the cheapest way
  to make the transfer revert.

### T-2 — High: a mint failure after the AVAX transfer refunds the same address again and again
- **Location:** `demo/tap.py` lines 83–91 (two independent transactions; nothing recorded between them) and
  line 89 (`build_transaction` with no `gas`, so `eth_estimateGas` runs and any revert raises a non-`TapError`).
- **Evidence:** Repro `test_mint_failure_after_transfer_repays_avax`: with the token pointed at a contract without
  `mint()` (i.e. a misconfigured `FOLIANT_TOKEN`, or any token whose mint is restricted, capped or paused), every
  call sends 0.02 AVAX, then raises `ContractLogicError` (a 500 from FastAPI, not a `TapError`), and the visitor's
  balance is `n × 0.02` after `n` calls with `per_hour=1` and `given == 0`. The same shape applies to any transient
  RPC error between the two sends.
- **Fix:** Record the address (and persist) *before* broadcasting the first transaction, with a status field
  (`{"given": {addr: "pending"|"done"}}`) so a crash or failure mid-give leaves the address ineligible rather than
  refundable; an operator can clear pending entries by hand. Send the mint first (it can be estimated and
  reverts cheaply) and the AVAX second, so the irreversible payout happens only after the fallible step. Wrap
  `build_transaction`/`estimateGas` in `try/except (ContractLogicError, ContractCustomError)` and convert to a
  `TapError`, as `foliant/chain.py` lines 273–277 already do.

### T-3 — High: a receipt timeout stalls the server, double-funds, and bricks the tap behind a stuck nonce
- **Location:** `demo/tap.py` `_send()` line 101 (`wait_for_transaction_receipt` with the default 120 s timeout,
  no exception handling) and line 82 (`get_transaction_count(..., "pending")`).
- **Evidence:** Repro `test_receipt_timeout_then_double_funding`: with automine off (a stand-in for a legacy
  `gasPrice` quote that a base-fee spike makes unmineable, or a slow public RPC), `give()` raises
  `TimeExhausted` (a 500), the transfer then mines anyway, `given == 0`, and a second `give()` pays the visitor a
  second time (balance 0.04). Because the next request takes the *pending* nonce it queues behind the stuck
  transaction, so every following tap also waits 120 s and fails — and with T-4 every one of those waits freezes
  the process. Nothing replaces or cancels the stuck transaction.
- **Fix:** Pass a short explicit `timeout` (e.g. 30 s) and catch `TimeExhausted` → `TapError(504, ...)` while
  still recording the address as pending (T-2). Use EIP-1559 fields (`maxFeePerGas` with headroom over the
  current base fee, `maxPriorityFeePerGas`) instead of a fixed legacy `gasPrice`, or at least quote
  `gas_price * 2`; the `need` check should use the same figure. Consider the "latest" nonce for the first send so
  a stuck predecessor is replaced rather than queued behind.

### T-4 — High: `POST /tap` blocks the event loop for the whole give
- **Location:** `demo/serve_chain.py` lines 83–94: `async def tap_endpoint` calls `tap.give(...)` directly.
- **Evidence:** `give()` makes at least four synchronous RPC round trips and two receipt waits. Because the route
  is a coroutine it runs on the loop, not in the threadpool. Repro `test_tap_endpoint_blocks_event_loop` (through
  `make_app()` and an ASGI client): with each `_send` taking 1.5 s, a concurrent `GET /health` — itself a sync
  route that normally answers in milliseconds — took 3.07 s. On Fuji that is ~4 s of total server freeze per
  successful tap, and up to 240 s per request on the T-3 path; `/infer` customers are frozen with it.
- **Fix:** `return await run_in_threadpool(tap.give, address)` (from `starlette.concurrency`) or make the route a
  plain `def`. The existing `threading.Lock` then does its job of serialising taps in the pool.

### T-5 — Medium: the hourly cap is not persisted, so a restart (or crash) reopens it
- **Location:** `demo/tap.py` line 44 (`_recent` in memory only) vs line 110 (only `given` is written).
- **Evidence:** Repro `test_hourly_cap_resets_on_restart`: `per_hour=1`, one give, second refused; a new `Tap` over
  the same state dir gives immediately. The docstring's "persisted, so a restart does not reopen it" is only true
  of the per-address rule. A visitor cannot force a restart, but T-3/T-4 make operator restarts likely exactly
  when the tap is under stress.
- **Fix:** Persist the recent timestamps alongside `given` (trim to the last hour on load).

### T-6 — Medium: `_persist` is not safe for two writers; the `flock` guards the wrong file
- **Location:** `demo/tap.py` `_persist()` lines 106–111; compare `ChainGate._save` / `_FileLock` in
  `foliant/chain.py` lines 451–475 and the `_load()` under lock at line 491.
- **Evidence:** The lock is taken on the `.tmp` file *after* `open(tmp, "w")` has already truncated it, so a
  second writer truncates the first writer's buffer before blocking, and after the first `os.replace` the
  second's `os.replace(tmp, ...)` raises `FileNotFoundError` (the tmp name is gone). More importantly the set is
  never re-read from disk, so two instances sharing `FOLIANT_STATE_DIR` (the dir the gate also uses) overwrite
  each other: repro `test_persist_lost_update_between_instances` — instance A gives `x`, instance B gives `y`, a
  fresh instance loads `given == 1` and funds `x` again. There is also no `fsync`, so after a power loss the
  file can be empty and `json.loads("")` then stops the server at startup (fail-closed, but by crash). Within the
  stated single-process deployment none of this bites today; it is a trap the moment a second worker or a second
  demo on the same network shares the directory.
- **Fix:** Reuse the gate's pattern: a separate lock file held for the whole give (`os.open` + `flock` on a
  `foliant-tap-<network>.lock`), reload `given` under that lock before deciding, write via
  `tempfile.mkstemp(dir=...)`, `fsync`, then `os.replace`. Drop the `flock` on the temp file.

### T-7 — Low: wrong-checksum mixed-case addresses are accepted and silently re-checksummed
- **Location:** `demo/tap.py` lines 64–66.
- **Evidence:** In web3 7.x `Web3.is_address` does not verify EIP-55 for mixed-case input (all five probes,
  including a swapped-case and a one-character-altered address, returned `True`), and `to_checksum_address`
  recomputes rather than validates. Repro `test_bad_checksum_is_accepted`: a case-mangled address is funded.
  The rate limit is *not* bypassed (the key is lower-cased), but a visitor's typo sends the tap's AVAX to an
  address nobody holds.
- **Fix:** `if address != address.lower() and address[2:] != address[2:].upper() and not Web3.is_checksum_address(address): raise TapError(400, ...)`.

### T-8 — Low: no guard against the tap key being another sender's key
- **Location:** `demo/tap.py` `tap_from_env`; `demo/serve_chain.py` line 60.
- **Evidence:** Nothing stops `FOLIANT_TAP_KEY == PROVIDER_KEY`. Both `Tap.give` and `ChainGate.settle` take the
  pending nonce under *different* locks, so a settle running in the threadpool while a tap sends would race to
  the same nonce and one transaction would be dropped as a replacement (then T-2/T-3 apply). The same happens if
  the operator sends from the tap wallet by hand while the server is up.
- **Fix:** `if tap.address.lower() == provider.lower(): raise SystemExit("FOLIANT_TAP_KEY must not be PROVIDER_KEY")`,
  and say in the docstring that the wallet must be used by this process only.

### T-9 — Low: `GET /chain` makes an RPC call per request for the tap balance
- **Location:** `demo/tap.py` `status()` line 60; `demo/serve_chain.py` line 81.
- **Evidence:** `status()` calls `eth_getBalance` every time. `/chain` is a sync route (threadpool), so it does not
  block the loop, but an unauthenticated GET now costs a call against a public Fuji RPC that rate-limits by IP.
  Address and balance themselves are public on-chain information; exposing them is fine.
- **Fix:** Cache the balance for ~30 s in `status()`, or drop `balanceWei` from `/chain` and keep it in a log line.

### Fine as is
- Address case: the key is `to.lower()` after `to_checksum_address`, so upper/lower/checksum variants all map to
  one entry (verified by `test_tap_funds_once_per_address` and by T-7's repro).
- In-process concurrency of the limit checks: `_given`/`_recent` are read and written under `self._lock` for
  the whole give, so once T-4 is fixed two overlapping requests cannot both pass the checks.
- Key handling: the key only lives in `Account.from_key`; no exception message, log line or response includes
  it, and `TapError` messages contain only a tx hash. Uncaught exceptions become a generic 500 body.
- Token binding: `tap_from_env` mints the same `FOLIANT_TOKEN` the offer sells, and the recipient is always the
  validated `to`; there is no way for a request to redirect the mint or change the amounts.
- Balance check before sending, so a dry wallet refuses with 503 and spends nothing (`test_tap_dry_wallet`).

## Tests (tests/test_tap.py)
- `test_tap_funds_once_per_address`: does what it says (case-insensitive, 429 on repeat, survives a new instance).
- `test_tap_rejects_bad_address`: does what it says, but misses the wrong-checksum case (T-7).
- `test_tap_hourly_cap`: tests the in-memory counter only; does not test that it survives a restart (T-5) or that
  a *failed* give counts (T-1).
- `test_tap_dry_wallet`: meaningful. Note it asserts `given == 0` "a failed tap leaves the address eligible" —
  that property is the root of T-1/T-2 and the test would need inverting once the address is recorded as pending.
- Missing entirely: any failure after the first broadcast (reverting recipient, failing mint, receipt timeout —
  the three repros above), concurrent gives, two instances on one state dir, a corrupt/empty state file, and the
  HTTP layer (`make_app()` + a client: 404 without a key, 400 on non-JSON, that `/tap` does not block other
  routes, that `/chain` carries `tap`). The scratch file shows a working pattern for the HTTP tests via
  `httpx.ASGITransport`; `make_app()` needs `FOLIANT_RPC/ACCOUNTS/CHANNELS/POOLS/TOKEN`, `PROVIDER_KEY`,
  `FOLIANT_TAP_KEY` and `FOLIANT_STATE_DIR` set to `tmp_path`.

## Round 2 (rewritten demo/tap.py, demo/serve_chain.py, tests/test_tap.py — uncommitted working tree)

`PYTHONPATH=. python3 -m pytest tests/test_tap.py -q`: 8 passed. Round-2 probes are in the scratch
`test_tap_review2.py` (4 tests, all pass, i.e. each observation below reproduces).

### Per-finding verdicts
- **T-1 (failed give free, no limit) — fixed.** `given[key]="pending"` and `recent.append` are written and
  fsynced before the nonce is even fetched (`give()` lines 98–101); `get_code` refuses contract recipients
  (line 92) before anything is charged. A pre-check refusal (bad input, contract, cap, dry) now costs the tap
  at most three RPC reads and nothing on chain; verified by `test_tap_rejects_bad_input` and
  `test_tap_dry_wallet_charges_nothing`.
- **T-2 (mint failure refunds AVAX) — fixed.** Mint first, AVAX second (lines 105–115); an estimateGas revert
  becomes `TapError(502)` with nothing broadcast; any exception after the charge is recorded as `failed:<reason>`
  and the address is 429 thereafter (`test_failure_after_broadcast_spends_the_address`). Re-ran the round-1
  "token without mint()" scenario mentally against the new order: the mint estimate reverts, nothing is sent,
  the address is spent. Nothing is free for the caller after a failure any more.
- **T-3 (receipt timeout → double funding, stuck nonce) — fixed for the double-funding, residual cost noted
  below (R2-1).** `TimeExhausted` → 504 with the address already spent, so a retry is 429; 25 % gas headroom
  makes a stuck legacy tx less likely. Probe R2-1 (automine off, `RECEIPT_TIMEOUT` patched to 1 s): visitor 1
  gets 504, visitor 2's mint takes the pending nonce behind the stuck one and also gets 504; both are
  `failed:` forever; when the chain catches up both hold 1 000 tokens and 0 AVAX (unusable without gas). So a
  stuck period no longer costs AVAX twice, but it burns every visitor who arrives while it lasts, at one per
  `RECEIPT_TIMEOUT` (or 2× if the mint confirms and the AVAX sticks). Nothing replaces or cancels the stuck tx.
- **T-4 (event loop blocked) — fixed.** `await run_in_threadpool(tap.give, ...)` (serve_chain.py line 95).
  Re-ran the round-1 loop-blocking probe shape against the new app: `/health` answers in milliseconds while a
  single give is in flight. See R2-3 for what happens with many.
- **T-5 (hourly cap not persisted) — fixed.** `recent` is saved with `given` and trimmed on each give;
  `test_tap_hourly_cap_persists` covers the restart.
- **T-6 (persist unsafe, flock on the wrong file) — fixed.** Sibling `.lock` opened with `O_CREAT|O_RDWR` and
  `flock`ed for the whole give (`_FileLock`, lines 149–166), `_load()` under the lock before every decision,
  `mkstemp` + `fsync` + `os.replace`. Confirmed the lock is held across both broadcasts: the `with` at line 83
  encloses the sends and the final `_save`. `test_two_instances_one_directory` is meaningful — two `Tap`
  instances have separate `threading.Lock`s, so only the file lock can produce the observed `["429", "ok"]`, and
  `flock` on separately opened descriptors conflicts within one process too.
- **T-7 (bad checksum accepted) — fixed, one edge (R2-4).** Mixed-case input must pass `is_checksum_address`;
  all-lowercase is accepted.
- **T-8 (shared key) — fixed.** `SystemExit` when the tap address equals the provider (`test_shared_key_refused`).
- **T-9 (RPC per GET /chain) — fixed.** Balance cached 30 s. `status()` now also takes both locks to reload the
  count, which is what makes it queue behind a give (see R2-3).

### New observations
- **R2-1 — Medium: a stuck transaction burns every visitor who arrives while it lasts.** Location: `give()`
  line 104 (`"pending"` nonce) and `_send()` line 142. Evidence above. Each subsequent give queues behind the
  stuck nonce, waits `RECEIPT_TIMEOUT`, marks its visitor `failed:` for good, and consumes an hourly slot.
  Since the lock is held throughout, the tap processes at most one such burn per 60 s, so the damage is bounded
  (≤ 60 burned visitors an hour, no AVAX lost), but each is a stranger who can never be funded by this tap
  again without the operator editing the state file. Fix: after a 504, do not take new attempts until the
  stuck nonce clears — keep a `stuck_until`/`stuck_nonce` in state and answer 503 "tap recovering" while
  `get_transaction_count(addr, "latest") <= stuck_nonce`; or on 504 immediately re-broadcast the same nonce
  as a 0-value self-transfer at `gasPrice*2` to cancel it. Either way, use `"latest"` for the first nonce so
  a recovered tap never queues behind a leftover.
- **R2-2 — Medium: a failure before the first broadcast still spends the address.** Location: `give()`
  lines 103–123 (`try` starts before the nonce fetch and the mint estimate; both `except` arms mark
  `failed:`). Probe R2-2: `get_transaction_count` raising `ConnectionError` (an RPC blip) → 502,
  `failed:ConnectionError` persisted, nothing on chain, and the visitor is 429 for ever. The same holds for the
  mint estimate reverting (a misconfigured or paused token would burn every visitor, one per attempt). Since
  nothing has left the process at that point, there is no replay risk in un-spending the *address*; the hourly
  slot should stay charged so a flapping RPC still cannot be used to spam. Fix: track `broadcast = False`,
  set it `True` immediately before the first `send_raw_transaction`, and in the `except` arms do
  `self._given.pop(key)` when `broadcast` is still `False` (keep `recent`), then `_save()`.
- **R2-3 — Medium: queued taps hold threadpool threads and starve every sync route.** Location:
  `serve_chain.py` line 95 (`run_in_threadpool`) + `give()` line 83 (blocking lock acquire) + `status()` line 74
  (takes the same locks). Probe R2-3 through `make_app()` and an ASGI client, `_send` patched to 0.25 s: with
  45 `POST /tap` in flight, `GET /health` (a sync `def`, normally ~1 ms) took 3.2 s — it had to wait for a
  free thread in anyio's 40-thread pool, all of which were parked on `self._lock`. On Fuji each give holds the
  lock ~4 s (two receipts), 60–120 s when stuck, so 40 cheap requests with fresh addresses freeze `/chain`,
  `/health`, `/settle` and anything else that uses the pool for minutes; unlike round 1 the loop itself stays
  alive, so `/infer` paths that are pure-async still respond. Fix: do not queue — `self._lock.acquire(
  blocking=False)` and answer 503 "another tap is in progress, retry in a few seconds" when it fails, and
  likewise `LOCK_EX|LOCK_NB` on the file lock; and have `status()` read the cached `len(self._given)` from the
  last give instead of taking the locks (a page view must never wait on a broadcast).
- **R2-4 — Low: the "all upper-case" exemption never matches the usual spelling.** Location: `_recipient()`
  line 134: `address != address.upper()` compares the whole string, so `0x` + upper-case hex (what a user
  means by "all caps") is treated as mixed case and refused with "bad checksum", while `0X` + upper hex is
  accepted. Probe R2-4 prints both. Harmless (the error tells them to lowercase it) but the docstring's promise
  is wrong. Fix: compare `address[2:]` to `address[2:].upper()`.
- **R2-5 — Low: the dry-wallet path still costs three RPC reads per fresh address.** `get_code`, `gas_price`
  and `get_balance` run before the 503 (lines 92–97), under the lock, for every request with an unseen address
  while the wallet is empty. No chain spend, but an unauthenticated way to burn a public RPC's per-IP quota;
  reuse the 30 s balance cache for the dry check and check the cap before `get_code`.
- **Fine:** key hygiene unchanged (persisted `failed:` strings carry only exception class names or web3 revert
  text); the state file name now includes the chain id and tap address so two networks or two keys never share
  a file; corrupt state file fails closed (500 per request) with both locks released by the context managers;
  `test_two_instances_one_directory` and `test_failure_after_broadcast_spends_the_address` are real tests of the
  claims they make; `test_http_layer` does not test "non-blocking" despite the module docstring (there is no
  concurrency assertion), and nothing exercises the real `TimeExhausted`/stuck-nonce path (R2-1 shows how with
  `evm_setAutomine`) or a pre-broadcast RPC error (R2-2).

**Closing.** All four Highs and both Mediums from round 1 are closed, and the tap can no longer be drained or
double-paid: a failure is now charged to the visitor rather than to the wallet. What remains is who pays for a
bad hour: a stuck transaction or an RPC blip permanently burns visitors (R2-1, R2-2), and a burst of requests
turns the held lock into a threadpool starvation of the other routes (R2-3). None of these loses testnet AVAX,
so the tap is fit to run on Fuji now; R2-2 and R2-3 are small changes worth making before it does, R2-1 can
wait for the first stuck transaction.

## Round 3 (working tree)

`PYTHONPATH=. python3 -m pytest tests/test_tap.py -q`: 11 passed. The round-2 probes were re-run unchanged
against the new code and now fail in the intended direction (each documented below); one new probe was added
to the scratch `test_tap_review2.py` (`test_stale_balance_cache_burns_last_visitor`) and reproduces R3-1.

### Verdicts
- **R2-1 (stuck tx burns following visitors) — fixed.** `give()` compares the `"latest"` and `"pending"` nonces
  before charging (lines 116–120) and answers 503 with nothing charged; the first send uses `latest`. Probe:
  visitor 1 behind a stuck tx gets 504 and is spent (correct: the mint was broadcast), visitor 2 now gets 503
  "transaction in flight" and remains eligible; `test_in_flight_transaction_refuses_new_attempts` covers the
  clear-after-mining half. Residual, by design: a transaction that never mines keeps the tap at 503 until the
  operator cancels it by hand; the message says so.
- **R2-2 (pre-broadcast failure spends the address) — fixed.** `broadcast` flag (lines 127–148): a failure
  before the first `send_raw_transaction` pops the address and keeps the hourly slot; RPC failures in the
  pre-checks are 502 with nothing charged (lines 95–96). Probe: `get_transaction_count` raising
  `ConnectionError` → 502, retry then succeeds ("DID NOT RAISE"); `test_failure_before_broadcast_releases_the_address`
  covers it in the suite.
- **R2-3 (queued taps starve the threadpool) — fixed.** `self._lock.acquire(blocking=False)` and
  `LOCK_EX|LOCK_NB` on the file lock both answer 503 immediately (lines 88–93, 186–193); `status()` takes no
  lock and reads the cached balance and last known count. Probe: 45 concurrent `POST /tap` with 0.25 s sends,
  `GET /health` answered in 0.13 s beyond its own 0.3 s start delay (was 3.2 s). The two-instance test's
  expected `["503", "ok"]` then 429 from both is the right observable.
- **R2-4 (upper-case exemption) — fixed.** `hexpart` comparison; `0x` + upper hex and `0X` + upper hex both
  accepted (probe), `test_uppercase_hex_accepted` in the suite.
- **R2-5 (dry path RPC reads) — fixed.** Cap checked before `get_code`; the cached balance is consulted first
  and a fresh read is made only when the cached figure is short (line 114).

### New observation
- **R3-1 — Medium (bounded to one visitor per time the wallet runs dry): the balance cache is not invalidated by
  the tap's own spending, so the last give before dry burns its visitor.** Location: `_give_locked()` line 114
  (`self._cached_balance() < need and self._cached_balance(0) < need`) and lines 136–142 (`broadcast = True`
  before `_send`). Evidence: probe with `avax_wei = 0.6 × balance`: give 1 succeeds and caches the pre-give
  balance; give 2 within 30 s passes the dry check on the stale figure, the mint (affordable) is broadcast, the
  AVAX transfer is rejected by the node as unaffordable (`Web3RPCError`), and the visitor is persisted as
  `failed:funding failed: Web3RPCError` holding 1 000 tokens and 0 AVAX, 429 for ever. With the real 0.02 AVAX
  gives the stale figure overstates by at most the gives made in the cache window (~7 on Fuji), so this fires
  once, for the visitor who arrives as the balance crosses `need`; it is exactly the case the 503 "dry" path was
  written for. Fix (one line): invalidate the cache after any broadcast — `self._balance_cache = (0.0, 0)` just
  before `self._given[key] = "funded"` and in the `except` arm — or subtract `need` from the cached figure on
  each give. Optionally also treat a `send_raw_transaction` rejection whose message contains "insufficient funds"
  as pre-broadcast (the node refused it, nothing entered the mempool) and release the address.
- **Low, hygiene:** if `_save()` raises inside the `except` arm (lines 143–150, disk full), the original error is
  replaced and the on-disk record stays `"pending"` while memory has released or failed it; the next `_load()`
  then treats the address as spent. Wrap that `_save()` so the original `err` is still raised. A corrupt state
  file now surfaces as `502 chain unavailable: JSONDecodeError` — fail-closed, but the message misleads the
  operator; log or special-case `_load()` errors. `_given_count` is updated only on a *funded* give, so
  `/chain`'s `given` lags failed attempts until the next success — cosmetic.

**Closing.** Round 3 closes everything raised in rounds 1 and 2 with the behaviour the fixes claim, verified by
re-running the earlier probes. The only remaining defect (R3-1) burns one visitor each time the wallet runs
down, is a one-line cache invalidation, and loses no AVAX; the two hygiene notes are optional. With R3-1 fixed
this is fit to run on the public Fuji server.

## Round 4 (working tree) — final

`PYTHONPATH=. python3 -m pytest tests/test_tap.py -q`: 12 passed. All eleven earlier probes (round-1 and
round-2 scratch files) were re-run once more against this tree: every attack probe now fails in the intended
direction — reverting recipient refused 400 before charging; mint-revert is a 502 with nothing sent and the
address released; receipt timeout is a 504 with the address spent and no second payout; the event loop and
the threadpool both stay responsive (`/health` 0.06 s during a give, 0.13 s beyond its start delay with 45 taps
queued); the hourly cap survives a restart; two instances agree; bad checksums are refused; a stuck tx makes
the next visitor a 503, not a burn; a pre-broadcast RPC error releases the address. The R3-1 probe
(`avax_wei = 0.6 × balance`) now gets `503 the tap is dry` on the second give with no tokens, no AVAX and no
state entry for the visitor.

### Verdicts
- **R3-1 (stale balance cache) — fixed.** `_balance_cache = (0.0, 0)` on every exit path of `_give_locked`
  (lines 149, 158, 170); `test_balance_cache_invalidated_by_own_spending` is the regression test.
- **NotBroadcast** (lines 43, 146–156, 187–192): a node rejection on the first send releases the address; after
  the mint went, it is spent. Correct for the "insufficient funds" case it was written for.
- **Hygiene — fixed.** `_save()` failure in the generic `except` arm is swallowed so the real error reports
  (165–168); a corrupt state file is `503 tap state file unreadable: <name>` (227–232); `_given_count` is
  updated on failed attempts too (154, 164).

### Residual, Low
- **`_send` line 191 treats every exception from `send_raw_transaction` as "not broadcast".** A transport error
  (`requests` `ReadTimeout`/`ConnectionError`) on a request the node *did* accept is then handled as if nothing
  left the process: on the first send the address is released while the mint is in the mempool. The in-flight
  nonce check returns 503 until that mint mines; afterwards the visitor may tap again and receive a second mint
  and the AVAX. Not attacker-triggerable, costs at most one extra 0.02 AVAX per RPC blip that lands exactly on
  the send call. Fix: catch only the node's structured rejection (`web3.exceptions.Web3RPCError`, which is what
  the insufficient-funds case raised in the probe) as `NotBroadcast`, and let transport errors fall through to
  the generic arm with `broadcast = True` already set (move line 141 above line 140, since the ambiguous case
  must count as sent). Also the `NotBroadcast` arm's `_save()` (line 155) lacks the `OSError` guard the generic
  arm has.

**Closing.** Four rounds in, every High and Medium from the review is closed and verified by re-running the
original reproductions rather than by reading the diff. The wallet cannot be drained, double-paid or made to
spend without funding anyone; a visitor cannot be burned by anything short of a transaction that reverts or
stalls after their mint was broadcast; and no request can hold the server. The one remaining item is a Low
about which exceptions count as "the node refused it". The tap is fit to run on the public Fuji server.
