# TAP-AUDIT-B — independent review of `demo/tap.py` at d9c7d03

Scope: `demo/tap.py`, its wiring in `demo/serve_chain.py`, `tests/test_tap.py`. Environment: web3 7.16.0,
eth-utils 6.0.0, hexbytes 2.0.0, anvil. Reproductions live in
`scratchpad/auditB/test_auditB.py` (13 tests, all passing against the code as committed; each finding below
names its test). Existing suite: `PYTHONPATH=. python3 -m pytest tests/test_tap.py -q` → 12 passed.

## Verdict

The tap's core invariants hold: one attempt per address (lower-cased key, persisted before broadcast, and
a crash between the two `_save()` calls leaves the address `pending`, which is treated as spent after a
restart); the hourly cap is shared and persisted; the file lock plus the non-blocking thread lock keep two
instances from funding one address twice and keep a busy tap from parking server threads; a dry wallet
and an in-flight nonce refuse before charging; `Web3RPCError` from `eth_sendRawTransaction` really is what
anvil/coreth return for a node rejection. I found no way for a stranger to drain beyond `per_hour × (0.02
AVAX + ~0.003 AVAX gas)` or to hang the server. What I did find is that two web3 7.x behaviours the code
relies on are not what it assumes: `eth_sendRawTransaction` **is** in web3's transport-retry allowlist,
so a lost response is re-sent and the node's "already imported" answer is then misread as "nothing was
broadcast" (B-1); and `wait_for_transaction_receipt` polls at 10 Hz, so one stuck transaction costs 600
RPC calls a minute (B-2). A stuck transaction also has no exit but the operator (B-3). Nothing High;
three Mediums, the rest hygiene.

## State at each exit of `give()`

| exit | memory `_given[key]` | disk | chain |
|---|---|---|---|
| bad input / contract / 429 / dry / in-flight / RPC error before charge | absent | unchanged | nothing |
| `_save()` at the charge point raises (B-5) | `"pending"` (+ hour slot) | unchanged (or absent) | nothing |
| mint `estimateGas` reverts, or any exception before `_send` | popped (hour slot kept) | popped | nothing |
| `NotBroadcast` on the mint | popped (hour slot kept) | popped | nothing **unless B-1** |
| `NotBroadcast` on the AVAX tx | `"failed:…"` | same | mint mined |
| `TimeExhausted` / status 0 / transport error on either send | `"failed:…"` | same | mint mined and/or tx in flight |
| crash (SIGKILL) after the first `_save()` | — | `"pending"` → refused after restart | whatever went |
| success | `"funded"` | same | both mined |

The in-memory state after an error path is always written back under the file lock (OSError on that
write swallowed, memory still right), so the two `_save()` calls plus `_load()` at the top of every attempt
keep two instances consistent; the temp-file + `os.replace` + fsync pattern means a reader under the lock
never sees a torn file.

## Findings

### B-1 (Medium) — web3 retries `eth_sendRawTransaction`; the retry's "already imported" is read as "not broadcast"

**Location:** `demo/tap.py` `_send()` lines 195–198; `NotBroadcast` handling lines 149–162.

**Evidence:** In web3 7.16 `REQUEST_RETRY_ALLOWLIST` (`web3/providers/rpc/utils.py`) ends with
`"eth_sendRawTransaction"`, and `HTTPProvider._make_request` retries up to 5 times on
`(ConnectionError, requests.HTTPError, requests.Timeout)`. The default HTTP timeout is 30 s. So when the
node accepts the signed transaction but the response is lost (a `ReadTimeout` on a loaded public RPC),
web3 re-sends the same raw bytes 125 ms later; anvil answers `{'code': -32003, 'message': 'transaction
already imported'}` (coreth: "already known"), which arrives as `Web3RPCError`, which `_send` turns into
`NotBroadcast`, which for the mint (`sent == 0`) pops the address. Effects: the visitor is told
"transaction rejected by the node" while their mint is in the mempool; the address is eligible again; the
in-flight check protects the nonce, but once the mint mines the visitor can `give()` again and be minted
(and funded) a second time. Reproduced in `test_b1_transport_error_on_send_is_retried_and_misread_as_not_broadcast`:
after one lost response the visitor holds 1,000 tokens with `given == 0`, then a second give succeeds and
they hold 2,000. Cost to the tap: one extra mint's gas per occurrence and a false error report; the hour
slot is charged twice for one visitor. Not a drain (bounded by `per_hour`), but a state/report error under a
realistic failure.

**Fix:** either build the ledger's provider with an `ExceptionRetryConfiguration` whose
`method_allowlist` excludes `eth_sendRawTransaction` (the tap does not own the provider, so do it in
`ChainLedger` or pass a configured `Web3` in), or make `_send` decide by the chain rather than by the
error class: on `Web3RPCError`, `if w3.eth.get_transaction_count(self.key.address, "pending") > tx["nonce"]:
raise TapError(502, "…accepted by the node but not yet confirmed…")` (i.e. treat as broadcast) else
`NotBroadcast`. The second is robust to any proxy that returns a JSON-RPC error after forwarding.

### B-2 (Medium) — receipt polling at 10 Hz: 600 RPC calls per stuck transaction, ~20 per block-time in the normal case

**Location:** `demo/tap.py` line 202 (`wait_for_transaction_receipt(h, timeout=RECEIPT_TIMEOUT)`).

**Evidence:** web3 7.16 `wait_for_transaction_receipt(..., poll_latency=0.1)` loops on
`eth_getTransactionReceipt` every 100 ms (source inspected; `test_b2_receipt_polling_rate` counts 20 polls
in a 2 s wait). With `RECEIPT_TIMEOUT = 60` a stuck transaction means 600 calls in a minute, and a give
whose both transactions are stuck means 1,200. On Fuji (~2 s blocks) even a healthy give makes ~20 polls
per transaction, so ~50 RPC calls per give against the shared public endpoint (`test_b2_normal_give_rpc_cost`
lists the 8 non-poll calls: getCode, gasPrice, getBalance, 2×getTransactionCount, estimateGas, chainId×2,
sendRawTransaction×2). The public Fuji RPC is rate limited per source IP; a burst of 10 req/s from the demo
server is the kind of thing that gets the whole server (including `/infer` settlement) throttled, and it is
triggered by exactly the condition the timeout is meant to survive.

**Fix:** `w3.eth.wait_for_transaction_receipt(h, timeout=RECEIPT_TIMEOUT, poll_latency=2)` (one poll per
Fuji block; 30 polls per stuck transaction instead of 600).

### B-3 (Medium) — a stuck transaction has no exit: every visitor gets 503 until the chain moves on

**Location:** `demo/tap.py` lines 120–124 (in-flight check), `_send` (no replacement/bump path).

**Evidence:** The nonce is always `latest`, and nothing in the tap ever sends a replacement at a higher
price. Once a tap transaction sits in a mempool (legacy `gasPrice` quoted at 1.25× `eth_gasPrice` can fall
under the base fee during a spike; or the load-balanced public RPC accepts it on a node that never
propagates it), the receipt wait gives up on that visitor after 60 s, the address is marked `failed`, and
every later request from anyone is refused "transaction in flight" — for as long as the node keeps the
transaction, with no self-healing and no operator endpoint to clear it. `test_b3_stuck_transaction_locks_tap_with_no_recovery`
shows five successive visitors and a restarted instance all refused while one tx is pending, and recovery
only when the chain includes it. The design note (R2-1) is right that refusing is better than queuing
behind it, but the aftermath needs an owner.

**Fix:** when the in-flight check trips and the stuck tx is older than, say, 2 × `RECEIPT_TIMEOUT` (store
the hash and time of the last broadcast in the state file), send a 0-value self-transfer with the same
nonce at `max(1.25 × current gas price, 1.1 × old price)` and wait for it, then proceed; or at minimum
document a one-line operator recipe (cast send --nonce N …) in the module docstring. Also consider
`type 2` transactions with a generous `maxFeePerGas` so a base-fee rise cannot strand the tap in the first
place.

### B-4 (Low) — malformed state file crashes start-up and is reported as "chain unavailable"

**Location:** `demo/tap.py` `_load()` lines 235–243.

**Evidence:** Only `json.loads` is guarded. A file whose top level is a list (`saved.get` →
`AttributeError`) or whose `recent` holds a non-number (`float("x")` → `ValueError`) escapes the
`except ValueError` (the comprehension is outside the `try`), and `give()` reports it as
`502 chain unavailable: AttributeError` while `__init__` raises an unnamed exception
(`test_b4_state_file_wrong_shape_is_reported_as_chain_unavailable`). Fail-closed either way, but the
operator is pointed at the chain instead of the file. The format itself is fine for upgrades: only the
keys of `given` are ever read, unknown keys are ignored, missing keys default; there is no version field.

**Fix:** widen the guard to `except (ValueError, TypeError, AttributeError)` around the whole parse and
validate shapes (`isinstance(saved, dict)`, `isinstance(saved.get("given"), dict)`), keep the 503 that names
the file; add `"version": 1` on write.

### B-5 (Low) — `_save()` failure at the charge point strands the address in memory

**Location:** `demo/tap.py` line 129.

**Evidence:** The charge-point `_save()` is outside the `try`, so an `OSError` (ENOSPC, EROFS) propagates to
`give()` as `502 chain unavailable: OSError` with `_given[key] = "pending"` and the hour slot left in
memory. If a state file already exists the next `_load()` discards them; on a fresh directory it does
not, and the address is refused (429) for the life of the process although nothing was broadcast
(`test_b5_save_failure_at_charge_leaves_address_stuck_in_memory`). Also the 502 text blames the chain.

**Fix:** wrap that `_save()`: on failure pop the key, drop the slot, raise `TapError(503, "tap state not
writable: <file>")`. Have `_load()` reset `_given`/`_recent` to empty when the file is absent.

### B-6 (Low) — `_FileLock.__enter__` leaks the fd on any error other than EWOULDBLOCK

**Location:** `demo/tap.py` lines 217–225.

**Evidence:** Only `BlockingIOError` closes the fd. `flock` raising `OSError(ENOLCK)` (state dir on NFS, or
lock table exhaustion) leaves the fd open and surfaces as `502 chain unavailable: OSError`; one fd per
request, forever (`test_b6_filelock_leaks_fd_on_unexpected_flock_error`: +10 fds for 10 requests). Same
pattern in `foliant/chain.py` `ChainGate._FileLock`.

**Fix:** `except BlockingIOError: …raise TapError(503)` / `except BaseException: os.close(fd); raise`.

### B-7 (Low) — the balance cache is bypassed while the tap is dry, and `/chain` now depends on the RPC

**Location:** `demo/tap.py` line 118; `demo/serve_chain.py` line 84 (`tap.status()`).

**Evidence:** `_cached_balance() < need and _cached_balance(0) < need` forces a fresh `eth_getBalance` on
every request while dry (`test_b7_dry_tap_bypasses_balance_cache`: 5 requests → 6 balance calls), which is
the state in which the operator least wants the server spending RPC quota; it also means an attacker can
make each request cost getCode + gasPrice + getBalance without consuming a slot (serialised by the lock, so
bounded to roughly one request per RPC round-trip). Separately, `GET /chain` was static; it now makes an
RPC call whenever the cache is older than 30 s (and after every give, which clears it), and `status()` has
no `try`, so an RPC hiccup turns `/chain` into a 500 and, during an outage, each `/chain` request ties up a
threadpool thread for web3's 5 retries × 30 s.

**Fix:** when the dry re-check fails, cache the low balance with a short TTL (say 10 s) instead of `(0.0,
0)`; in `status()`, catch exceptions from `_cached_balance()` and report the last known figure (or `null`).

### B-8 (Low) — `_recipient` slices `[2:]` on inputs `is_address` accepts without a `0x` prefix

**Location:** `demo/tap.py` lines 186–191.

**Evidence:** eth-utils 6 `is_address` accepts bare 40-hex (`"5aAeb…"`) and `0X…`; the checksum branch then
inspects `address[2:]` (two hex characters short) and `is_checksum_address` returns False for both forms,
so a correctly checksummed address given without `0x`, or with `0X`, is refused with "bad checksum (a
typo?)" (`test_b8_unprefixed_checksummed_address_rejected_as_bad_checksum`). Only a usability/message
error; nothing is charged.

**Fix:** normalise first: `if not address.lower().startswith("0x"): address = "0x" + address`, then
`hexpart = address[2:]`; or simply require `address[:2] == "0x"`.

## Cost model

Per successful give: 8 non-poll RPC calls + ~10 receipt polls per second of block time (B-2); gas ≈ 21,000 +
~50–70k (mint to a fresh balance slot; the 80,000 budget is adequate) at 1.25 × `eth_gasPrice`, i.e. ≈
0.003 AVAX at 25–30 nAVAX; total ≈ 0.023 AVAX per give, so at `per_hour = 20` the maximum drain is ≈ 0.46
AVAX/h ≈ 11 AVAX/day and a 100 AVAX wallet lasts ~9 days under continuous abuse — by design, and testnet
only. Requests refused at the address or hourly check cost no RPC calls, one file read+parse (the file grows
by one line per attempt ever, ≤ 480/day; fine). Requests refused at getCode / dry / in-flight cost 1–5 RPC
calls and no slot, serialised by the thread lock. Note that `MockERC20.mint` is unpermissioned, so the mint
is ~70 % of the tap's gas per give for something a visitor can do themselves once they have AVAX; if RPC
quota or gas matters, `FOLIANT_TAP_TOKENS=0` and a `mint` hint in the response would halve the cost.

## Checked and fine

- `Web3RPCError` is what web3 7.16 raises for a node rejection of `eth_sendRawTransaction` (anvil:
  -32003 "Insufficient funds…", "nonce too low", "transaction already imported"); a transport error is not
  a `Web3RPCError` and is correctly treated as ambiguous (`test_ok_node_rejection_after_mint_marks_failed`).
- `wait_for_transaction_receipt` raises `TimeExhausted` (not `TransactionNotFound`) at timeout and
  swallows `TransactionNotFound`/`TransactionIndexingInProgress` while polling; `r["status"]` is the receipt
  status; `hexbytes` 2.0 `.hex()` has no `0x`, so the `"0x" +` prefix is right.
- `get_transaction_count(addr, "pending")` differs from `"latest"` while a tx is in the mempool (anvil; coreth
  implements the pending tag from the txpool), so the in-flight check works; `nonce = latest` is right after
  the check passes.
- Charge-before-broadcast and the crash window: a SIGKILL after the first `_save()` leaves `"pending"`,
  refused after restart (`test_ok_crash_after_charge_fails_closed`); a crash between charge and
  `estimateGas` loses that address its attempt — fail-closed, acceptable.
- File lock: `flock` on a separate open() per attempt is per open-file-description, so the two-instance test
  is equivalent to two processes; non-blocking mode raises `BlockingIOError` on Linux and is mapped to 503
  without waiting; `_load()` under the lock on every attempt merges the other instance's `recent` and
  `given`; temp-file + fsync + `os.replace` never exposes a torn file. The gate uses a different lock/state
  file so the two do not contend.
- Threading: `threading.Lock` non-blocking then `flock` non-blocking; `give` runs via `run_in_threadpool`, at
  most one thread is ever inside a chain round-trip, `status()` and a second `give` return in < 0.5 s while
  one is waiting on a receipt (`test_ok_status_does_not_block_on_give`). Client disconnects do not cancel
  the give; state stays right, the visitor's retry gets 429 (they were funded).
- Recipient checks: `get_code` refuses contracts and EIP-7702 delegated EOAs; a CREATE2 race or a
  precompile/zero-address recipient can only make the AVAX step fail after the mint, spending the attacker's
  own slot and ≤ one normal give's gas.
- `build_transaction` with `gasPrice` set does not add EIP-1559 fields; `sign_transaction` accepts the
  `from` field; `chainId` is pinned so signed txs cannot replay elsewhere; the tap key is refused if it equals
  `PROVIDER_KEY`.
- HTTP layer: non-JSON → 400, non-dict/absent address → 400 via `_recipient`, no tap → 404, `TapError`
  status passed through; nothing else is caught, so an unexpected exception is a 500 (fine).
- No secrets in error text; the state file and lock are 0600.

## Test coverage

`tests/test_tap.py` covers the invariants that matter most (once-per-address persisted, hourly cap
persisted, contract refusal, failure-after-broadcast spends, two instances, pre-broadcast release,
in-flight refusal, dry wallet, balance-cache invalidation, HTTP layer). It would not catch regressions in:
the `NotBroadcast` path (no test makes the node reject a send), receipt status 0, a real `TimeExhausted`
(simulated by raising `TapError` in a patched `_send`, which bypasses `_send` entirely), the retry/re-send
behaviour of B-1, poll frequency (B-2), the post-stuck lock-out (B-3), state-file shape (B-4), the
charge-point `_save()` failure (B-5), fd handling in `_FileLock` (B-6), `/chain` under a running give,
un-prefixed input (B-8), or `tap_from_env` parsing. The scratchpad tests in
`scratchpad/auditB/test_auditB.py` cover each of these and can be lifted into `tests/test_tap.py` (the
`HTTPProvider.make_request` counter and the `HTTPSessionManager.make_post_request` lossy wrapper are the
two helpers they need).
