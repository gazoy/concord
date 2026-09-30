# TAP-AUDIT-A — independent review of the testnet tap (commit d9c7d03)

Scope: `demo/tap.py`, its wiring in `demo/serve_chain.py`, `tests/test_tap.py`. Reviewed without reading
`demo/TAP-REVIEW.md`. Reproductions live in
`/tmp/claude-0/-home-claude/f52e2597-e0d8-5b12-943c-449c632a675a/scratchpad/auditA/test_repro.py`
(12 tests, all pass against Anvil; run with `PYTHONPATH=/home/claude/concord python3 -m pytest -q <file>`).
Shipped suite: `PYTHONPATH=. python3 -m pytest tests/test_tap.py -q` → 12 passed.

## Verdict

The core accounting is sound: the per-address and per-hour limits are checked before any RPC call, charged and
persisted before anything is broadcast, guarded by a thread lock plus a non-blocking `flock` so two instances
over one directory cannot double-fund, and the `pending != latest` nonce guard stops a stuck transaction from
eating the next visitor's attempt. I found no way for an anonymous visitor to bypass the per-address rule, to
double-spend a slot, or to park a server thread. What the design does not defend against is the obvious one:
the only limits are per-address (free to mint) and global, so one client can burn the whole hourly budget into
wallets it controls in seconds and keep every other visitor at 429 indefinitely (A-1, High). Two Medium issues
are correctness-under-flakiness, not visitor-exploitable: a transaction that outlives the 60 s receipt wait
bricks the tap until an operator hand-replaces it, and the visitor is recorded as failed although the transfer
lands (A-2); and web3 7.x silently retries `eth_sendRawTransaction`, so a lost response is turned into a
"node rejected it, nothing in flight" verdict that releases an address whose mint is already on chain (A-3).
The rest is hygiene. Ship after A-1 and A-3; A-2 needs at least an operator procedure.

## Findings

### A-1 (High) — no per-client limit: one visitor monopolises the tap and drains the hourly budget at will
- Location: `demo/tap.py` `_give_locked` lines 108–113; `demo/serve_chain.py` `/tap` (no client identity used).
- Evidence: `test_a1_one_visitor_exhausts_the_global_cap`, `test_a1_http_layer_has_no_per_client_limit`. With
  `per_hour=4` one `TestClient` gets `200,200,200,200,429` for four freshly generated addresses; the next
  honest visitor gets 429. `Account.create()` is free, so the per-address rule costs the attacker nothing.
  Default cap 20/h → 0.4 AVAX + gas per hour, ~10 AVAX/day, all to attacker-controlled keys and sweepable;
  and, more to the point for a demo, every other visitor is locked out for as long as the attacker bothers,
  with a one-line loop. The 503-if-busy design also means the attacker's steady stream of requests keeps the
  tap "serving someone else" for honest callers even before the cap is hit.
- Fix: add a second, per-client dimension to the same persisted state: e.g. `FOLIANT_TAP_PER_IP_PER_DAY=2`
  keyed by a hash of the client address (take `X-Forwarded-For` only if `FOLIANT_TRUST_PROXY` is set, else
  `request.client.host`), checked in `_give_locked` alongside the hourly cap and charged at the same point.
  Add a global daily cap (`FOLIANT_TAP_PER_DAY`) so a rotating-IP attacker is bounded per day, not per hour.
  Consider lowering the default hourly cap to 5. Pass the client key into `give(address, client)`.

### A-2 (Medium) — a stuck transaction bricks the tap indefinitely and the visitor is misrecorded
- Location: `demo/tap.py` `_send` lines 201–204; `_give_locked` lines 120–124, 163–175.
- Evidence: `test_a2_stuck_transaction_bricks_the_tap_and_misreports`. With mining paused and
  `RECEIPT_TIMEOUT=1`: the visitor gets 504 and is persisted as `failed:…`; every subsequent visitor gets 503
  "in flight" for as long as the transaction sits in the pool; when it finally mines, the "failed" visitor has
  the 1,000 tokens but no AVAX, and their retry is 429. On Fuji this is the gas-spike case: a legacy
  `gasPrice` at 1.25× the quote can sit below base fee for far longer than 60 s, and nothing in the code
  bumps or replaces it. The operator has no procedure or tool for it, and the state file cannot be
  reconciled (`failed:` looks the same for "never landed" and "landed late").
- Fix: on `TimeExhausted` send a replacement with the same nonce (to self, value 0, `gasPrice*1.5`) and
  wait a second window, so the nonce clears either way; if that also times out, persist
  `stuck: {nonce, hash, since}` and refuse with a message that names it. Record the visitor as
  `unconfirmed:<hash>` rather than `failed:` so an operator can reconcile later. Document the manual
  replace-by-fee procedure (`cast send --nonce N --gas-price …` to self) in the module docstring.

### A-3 (Medium) — web3 retries `eth_sendRawTransaction`, so "rejected by the node" can mean "already in flight"
- Location: `demo/tap.py` `_send` lines 195–198 and the `NotBroadcast` handling at 149–162.
- Evidence: web3 7.16 `providers/rpc/utils.py` default `method_allowlist` includes `eth_sendRawTransaction`;
  `HTTPProvider._make_request` retries it up to 5 times on `ConnectionError`/`Timeout`.
  `test_a3_transport_timeout_on_send_is_misclassified_as_not_broadcast`: the first send reaches the node, the
  response is lost, the retry gets a nonce/already-known `Web3RPCError`, `_send` raises `NotBroadcast`,
  `sent == 0` so the address is **released** (`given == 0`, absent from the file) and the visitor is told
  "transaction rejected by the node" — while the mint is on chain. The visitor then gets funded a second time
  (2,000 tokens, an extra mint's gas). The docstring's invariant "one attempt per address, ever" and the
  comment "the node answered and refused it: nothing is in flight" are both false under this retry.
  Not visitor-triggerable (needs a server↔RPC transport failure), but a public RPC endpoint drops responses
  routinely.
- Fix: give the tap a provider with the retry allowlist minus `eth_sendRawTransaction` (or set
  `exception_retry_configuration=None` on a dedicated `HTTPProvider` for the tap key), and in `_send` treat
  a `Web3RPCError` whose message contains `nonce too low` / `already known` / `already imported` as
  broadcast (raise `TapError(502)` not `NotBroadcast`). Either alone closes the hole; do both.

### A-4 (Low) — precompile addresses pass the EOA check and make the wallet spend gas funding nobody
- Location: `demo/tap.py` line 114 (`get_code`) and the fixed `gas: 21_000` transfer at line 147.
- Evidence: `test_a4_precompile_address_spends_gas_and_funds_nobody`. `0x…01` (ecrecover) has no code; the
  mint goes, then the 21,000-gas value transfer executes the precompile and runs out of gas: status 0,
  "funding transaction reverted", wallet paid for two transactions, no AVAX delivered. Bounded (nine EVM
  precompiles plus Avalanche's `0x0100…`/`0x0200…` ranges; each burns one address's attempt), so Low.
- Fix: `if w3.eth.estimate_gas({"from": self.key.address, "to": to, "value": self.avax_wei}) != 21_000:
  raise TapError(400, "…")` before charging; or refuse `int(to, 16) < 2**16` and the Avalanche ranges.

### A-5 (Low, operator) — state-file handling: non-object JSON, whole-server failure, no runtime reset
- Location: `demo/tap.py` `_load` lines 235–243, `__init__` 64–66; `demo/serve_chain.py` line 61.
- Evidence: `test_a5_state_file_that_is_valid_json_but_not_an_object`: `[]` or `null` is valid JSON, so the
  `except ValueError` does not fire; `Tap()` raises a bare `AttributeError` and `give()` reports
  "chain unavailable: AttributeError" (the chain is fine); `"recent": ["soon"]` → "chain unavailable:
  ValueError". `test_a5_corrupt_state_file_stops_the_whole_server_starting`: a truncated file makes
  `make_app()` raise, taking `/infer`, `/chain`, `/settle` down with the tap. Also: `_load` only overwrites
  in-memory state when the file exists, so deleting the file to "reset" the tap on a running server does
  nothing until the next `_save` re-creates it from memory; failed saves leave `foliant-tap-…jsonXXXX` temp
  files; `given` grows without bound and the whole file is re-parsed and fsync'd on every attempt.
- Fix: in `_load`, validate shape (`isinstance(saved, dict)`, `given` a dict of str→str, `recent` a list of
  numbers) and raise `TapError(503, …)` on any of `ValueError, TypeError, AttributeError, OSError`; clear
  in-memory state when the file is absent. In `serve_chain.py` catch `TapError` from `tap_from_env` and
  start with `tap = None` plus a loud log line (or make the fail-closed choice explicit in the docstring).
  Prune `given` entries older than, say, 90 days if "ever" is not literally required.

### A-6 (Low) — GET /chain fails when the tap's balance RPC fails
- Location: `demo/tap.py` `status`/`_cached_balance` lines 75–86; `demo/serve_chain.py` line 84.
- Evidence: `test_a6_get_chain_fails_when_balance_rpc_fails`: once the 30 s cache has expired, a failing
  `eth_getBalance` makes `/chain` (the endpoint agents use to find the contract addresses) return 500. The
  cache is not refreshed on failure, so every `/chain` call during an outage makes an RPC call, and
  concurrent callers after expiry all hit the RPC (no single-flight).
- Fix: wrap the balance read in `status()` and return `"balanceWei": None` on error, caching the failure
  for a few seconds.

### A-7 (Low) — `status()` and `give()` share `_balance_cache`; a racing `/chain` read can hide the spend
- Location: `demo/tap.py` lines 75–80, 118, 152/164/176.
- Evidence: `test_a7_stale_high_balance_cached_by_status_lets_the_mint_go_then_the_transfer_fail`. A
  `status()` read that starts before the tap's own transfer mines and stores after `give()`'s reset leaves a
  stale-high figure; the next `give()` trusts it (the fresh recheck runs only when the cache says dry),
  broadcasts the mint, and the AVAX send is refused by the node: the visitor is marked `failed:` with tokens
  and no gas. Anyone can hammer `/chain`, but it needs the wallet to be within one grant of dry, so Low.
- Fix: `give()` should read the balance fresh (one `eth_getBalance` next to two transactions is free) and
  never consult the status cache; keep the cache for `status()` only.

### A-8 (Low) — hygiene
- `demo/serve_chain.py` line 91: `await request.json()` buffers and parses an unbounded body on the event
  loop (`test_hyg_unbounded_body_is_buffered_and_parsed`: 32 MiB accepted, then 400). Same as `/infer`, and
  only a memory problem at gigabyte scale, but check `content-length` and reject > 1 KiB with 413 before
  reading. If uvicorn is exposed without a reverse proxy, this and A-1 both get worse.
- `give()` line 99 wraps every non-`TapError` as "chain unavailable: <ExceptionName>", including bugs and
  file errors (A-5); reserve that message for RPC exception types.
- `_recipient`: `Web3.is_address` accepts a bare 40-hex string, so `address[2:]` strips two hex digits and
  the mixed-case checksum guard is applied to the wrong substring. Harmless (the key is still lowercased and
  consistent), but require the `0x` prefix explicitly.
- A `give()` can take several minutes (two 60 s receipt waits plus 30 s RPC timeouts); a reverse proxy with
  a 60 s read timeout will 504 the visitor while the server completes the funding, and their retry is 429.
  Document the required proxy timeout or return early with the hashes after the second broadcast.
- `_recent` slots are kept on a pre-broadcast failure by design; note that an RPC blip during `estimateGas`
  therefore consumes hourly capacity with nobody funded.

## Checked and fine
- Per-address and hourly checks run before any RPC call and before charging; refused requests (bad address,
  already funded, cap reached) cost no RPC and no gas, so there is no RPC amplification.
- Charge-then-broadcast order and persistence: `given[key] = "pending"` and the `recent` slot are saved
  (mkstemp + fsync + `os.replace`) before the first `sendRawTransaction`; a crash anywhere after that leaves
  the address spent, never re-eligible.
- Locks: thread lock then `flock`, both non-blocking, released in `finally`; the lock fd is closed on
  `BlockingIOError`; the state is re-read under the file lock on every give, so two instances over one
  directory agree (re-verified). Lock file names cannot collide with `ChainGate`'s.
- Case handling: keys are `to.lower()`, so checksum/upper/lower variants of one address cannot double-fund;
  the checksum guard is only applied to mixed-case input.
- Nonce: `latest` is used and the `pending != latest` guard refuses while anything of this key is in flight;
  `FOLIANT_TAP_KEY == PROVIDER_KEY` is refused at startup; nothing else in the process signs with the key.
- Mint before AVAX with `estimateGas` as the revert guard; contract recipients refused via `get_code`
  (also catches EIP-7702-delegated EOAs).
- `POST /tap` runs `give()` in the threadpool; a busy tap answers 503 immediately rather than parking a
  thread; `/chain` and `/health` are sync routes (threadpool). Non-JSON, non-object and non-string bodies
  → 400; no tap → 404.
- `GET /chain` exposes only the tap address, cached balance, the two amounts, the cap and a count — nothing
  a visitor cannot read from the chain.
- Anvil raises `Web3RPCError` for insufficient funds and for a duplicate send, so the `NotBroadcast`
  classification is right whenever the node genuinely answered first time.

## Test coverage
`tests/test_tap.py` (12 tests) proves the per-address rule, the persisted hourly cap, the dry refusal, the
post-broadcast "spent" marking, two-instance agreement, the in-flight guard and the HTTP status codes.
Names overstate in two places: `test_failure_before_broadcast_releases_the_address` fails in the pre-checks
(before the address is charged), so the actual release path after charging (`_given.pop` at lines 156 and
169) is never executed by the shipped suite — my `test_cov_*` pair covers both and they behave as intended;
and `test_http_layer` says "non-blocking" but only checks status codes. Nothing exercises a `Web3RPCError`
from the node, a receipt timeout whose transaction later lands (A-2), `status()` racing `give()` (A-7), or
malformed-but-valid-JSON state (A-5).
