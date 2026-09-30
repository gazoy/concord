# TAP-AUDIT-FINAL — adjudication of TAP-AUDIT-A and TAP-AUDIT-B (commit d9c7d03)

Scope: `demo/tap.py`, `demo/serve_chain.py`, `tests/test_tap.py`, with `foliant/chain.py` for context. I re-ran
both reproduction suites as committed (A: 12 passed; B: 13 passed; shipped `tests/test_tap.py`: 12 passed), read
every reproduction to check what it actually asserts, verified the web3 7.16 claims against the installed source
(`providers/rpc/utils.py` REQUEST_RETRY_ALLOWLIST ends with `eth_sendRawTransaction`; default retry errors are
`ConnectionError, requests.HTTPError, requests.Timeout`, 5 retries; `wait_for_transaction_receipt(..., poll_latency=0.1)`),
and wrote two extra checks of my own (scratchpad `final/test_final.py`, 2 passed) where an audit's evidence was
indirect. `demo/TAP-REVIEW.md` was not read.

Severity is judged for what this is: a public testnet tap whose only asset is faucet AVAX plus unpermissioned
mock tokens, whose purpose is letting strangers try the protocol. Nothing here puts real value at risk. The
questions that matter are (a) can a stranger stop other strangers using it, (b) does it wedge itself, and (c)
does it lie to the visitor or the operator about what happened.

## Verdict

The update is sound where it needs to be: the per-address and per-hour rules are checked and persisted before
any broadcast, the thread lock + non-blocking `flock` keep two instances from double-funding, refused requests
cost no RPC, and neither auditor nor I found a way for a visitor to bypass the per-address rule or park a
server thread. It is not ready for a public server as committed, for three reasons that are all cheap to fix:
the tap has no per-client dimension at all, so one loop of fresh addresses locks everyone else out for an hour,
repeatedly (A-1); web3's transparent retry of `eth_sendRawTransaction` turns a lost response into "rejected,
nothing in flight", which releases an address whose mint is on chain and breaks the documented one-attempt
invariant (A-3/B-1, identical finding, both correct); and a transaction that outlives the receipt window wedges
the tap for every visitor with no recovery path and a misleading `failed:` record (A-2/B-3, identical). The
remaining findings are correctness-under-flakiness and hygiene; each is real, none is urgent. Six of the
sixteen findings are duplicates across the two reports, which is itself good evidence that both auditors were
looking in the right places.

## Rulings on every finding

| Finding | Claim | Ruling | Audit sev → final | Why |
|---|---|---|---|---|
| A-1 | No per-client limit; one visitor drains the hourly budget and locks everyone out | CONFIRMED | High → **Medium** | Both repros pass and the mechanism is plain in the code. Downgraded because the asset is faucet AVAX, the hourly cap already bounds the loss (≈0.46 AVAX/h), and any per-IP limit is porous. It stays at the top of the fix list because the harm that remains — a one-liner makes the demo unusable for everyone — is exactly the failure a public demo cannot afford, and the fix is small. B did not raise the lock-out at all (its cost model treats the drain as "by design", which is fair, but misses that the drain *is* a denial of service to honest visitors). |
| A-2 | Stuck tx bricks the tap; visitor misrecorded | CONFIRMED | Medium → Medium | Same as B-3. Repro shows 504 → `failed:` → every later visitor 503 → tokens land, no AVAX, retry 429. Severity right: low probability on Fuji (legacy price at 1.25× the quote rarely falls under base fee), total consequence when it happens, no operator tool. |
| A-3 | web3 retries `eth_sendRawTransaction`; retry's error misread as not-broadcast; address released | CONFIRMED | Medium → Medium | Same as B-1. Library claim verified in source; repro is faithful (the request reaches the node, then the response is dropped once). Result: visitor told "rejected", `given == 0`, second give succeeds, 2,000 tokens. Not visitor-triggerable, but public RPCs drop responses routinely. |
| A-4 | Precompile addresses pass `get_code` and burn gas funding nobody | CONFIRMED | Low → Low | Repro passes: mint goes, 21,000-gas transfer to `0x…01` runs out of gas, status 0, nothing delivered. Bounded by the handful of precompile addresses, each costing one slot. B lists this under "checked and fine" as an acceptable cost; A's severity is right and the `estimate_gas != 21_000` guard is cheap, but this can wait. |
| A-5 | State file: non-object JSON escapes the guard, corrupt file stops the whole server, no runtime reset, temp-file litter | CONFIRMED | Low → Low | Same as B-4, with more: A also shows a truncated file makes `make_app()` raise (so `/infer`, `/chain`, `/settle` go down with an optional feature) and that deleting the file on a running server does not reset (the next `_save` re-creates it from memory). All verified by A's tests and by reading `_load`. |
| A-6 | `GET /chain` 500s when the balance RPC fails | CONFIRMED | Low → Low | Same as B-7's second half. Repro passes; `status()` has no `try`. `/chain` is the address-discovery endpoint agents need, so this is worth one `try`, but it only bites during an RPC outage in which `/infer` is down anyway. |
| A-7 | `status()` and `give()` share `_balance_cache`; a stale-high figure lets the mint go and the AVAX send fail | PARTLY → CONFIRMED with a different path | Low → Low | A's repro injects the stale cache by hand and its single-instance race window is narrow (the `status()` thread must be preempted between the `get_balance` return and the cache store for the whole tail of a `give()`). But I found a race-free path: two instances over one wallet (the supported deployment) — instance A's `status()` caches, instance B spends, instance A's `give()` trusts the cache. My `test_a7_two_instances_stale_balance_cache_no_race_needed` reproduces the exact outcome (mint mined, AVAX refused, visitor `failed:`). Needs the wallet within one grant of dry; Low is right; fix is one line. |
| A-8 | Hygiene: unbounded body, error wrapping, `_recipient` slicing, proxy timeouts, slot kept on pre-broadcast RPC blip | CONFIRMED | Low → Low | Body test passes (32 MiB buffered then 400). `_recipient` item = B-8. All accurate. |
| B-1 | web3 retries `eth_sendRawTransaction`; "already imported" read as not-broadcast | CONFIRMED | Medium → Medium | = A-3. B's write-up is the more precise of the two (names the anvil error code, the 125 ms backoff, and that the hour slot is charged twice). |
| B-2 | Receipt polling at 10 Hz: 600 calls per stuck tx, "1,200 per give" | PARTLY | Medium → **Low** | `poll_latency=0.1` verified; the 20-polls-in-2 s test passes. But "a give whose both transactions are stuck means 1,200" is wrong: the AVAX transfer is only sent after the mint's receipt, so at most one window per give is spent on a stuck tx (my `test_b2_only_first_tx_can_be_stuck…`: 1 `eth_sendRawTransaction`, ~10 receipt polls in a 1 s window). And a stuck tx is followed by every other visitor being refused *before* any send, so the burst happens once, not per visitor. ~50 RPC calls per healthy give against a shared public endpoint is still a poor use of quota, and the fix is one keyword argument — do it, but it is quota hygiene, not a Medium. |
| B-3 | Stuck tx has no exit | CONFIRMED | Medium → Medium | = A-2. B's repro adds the point that a restart does not help (the state is on chain, not in the file). |
| B-4 | Malformed state file crashes start-up and is reported as "chain unavailable" | CONFIRMED | Low → Low | = A-5 (subset). |
| B-5 | `_save()` failure at the charge point strands the address in memory | CONFIRMED | Low → Low | Repro passes (fresh directory, ENOSPC → 502 "chain unavailable: OSError", then 429 for that address for the life of the process with nothing broadcast). Real, exotic, fail-closed; trivial fix. A missed it. |
| B-6 | `_FileLock.__enter__` leaks the fd on any error other than `BlockingIOError` | CONFIRMED | Low → Low | Repro passes (+10 fds for 10 requests under simulated ENOLCK). Same pattern in `foliant/chain.py` `ChainGate._FileLock` (lines 459–475, blocking, so only a non-EWOULDBLOCK error leaks there). Exotic; one `except BaseException: os.close(fd); raise`. A missed it. |
| B-7 | Dry tap bypasses the cache on every request; `/chain` now depends on the RPC | CONFIRMED | Low → Low | Both halves verified (5 requests → ≥5 `eth_getBalance` while dry; `/chain` 500 = A-6). B's note that a dry-tap request costs `getCode + gasPrice + getBalance` with no slot consumed is accurate but the thread lock serialises it, so it is bounded. |
| B-8 | `_recipient` slices `[2:]` on inputs `is_address` accepts without `0x` | CONFIRMED | Low → Low | = A-8 bullet 3. Repro passes: a correctly checksummed address without `0x` is refused "bad checksum". Message-only. |

Both audits' "checked and fine" sections agree with each other and with my reading of the code; I did not find a
"fine" claim that is wrong.

## Merged, ranked list

Where A and B found the same thing I name both and pick one fix.

### Fix before the tap goes on a public server

**F-1 (Medium) — no per-client limit: one client locks out everyone** — A-1 only.
Fix (A's, adopted): add a per-client counter to the same persisted state, keyed by a hash of the client address
(`X-Forwarded-For` only when `FOLIANT_TRUST_PROXY` is set, else `request.client.host`), checked in `_give_locked`
next to the hourly check and charged at the same point; add a global daily cap (`FOLIANT_TAP_PER_DAY`) so a
rotating-IP client is bounded per day, not per hour; pass the client key into `give(address, client)`. Lowering
the default hourly cap is optional. Do not pretend this is airtight — it raises the cost of the one-line loop,
which is the goal.

**F-2 (Medium) — web3 retries `eth_sendRawTransaction`; the retry's "already imported"/"nonce too low" is read as
"not broadcast" and the address is released** — A-3 = B-1.
Fix (B's, adopted): decide by the chain, not by the exception class. In `_send`, on `Web3RPCError` check
`w3.eth.get_transaction_count(self.key.address, "pending") > tx["nonce"]`; if so the transaction is in the
pool, raise `TapError(502, "...accepted by the node but not yet confirmed...")` (the caller's `broadcast=True`
path marks the address spent); otherwise `NotBroadcast` as now. This is local to `tap.py`, needs no change to
the shared provider that `ChainLedger` owns (A's allowlist change would also alter the gate's settle behaviour),
and is robust to any proxy that returns a JSON-RPC error after forwarding. A's string-match on the error message
is not adopted (node-specific text). Optionally *also* disable the retry for `eth_sendRawTransaction` on the
tap's provider as A suggests; not required once the chain check is in.

**F-3 (Medium) — a stuck transaction wedges the tap for everyone with no exit, and the visitor is recorded
`failed:` although the mint lands** — A-2 = B-3.
Fix (merged): the minimum before public is (i) record the visitor as `unconfirmed:<hash>` rather than
`failed:` on `TimeExhausted` (A) so the state file can be reconciled against the chain, (ii) persist
`stuck: {nonce, hash, since}` and name it in the 503 (A), and (iii) document the one-line operator recipe
(`cast send --nonce N --gas-price … <tap address> --value 0`) in the module docstring (both). The self-healing
replacement — when the in-flight check trips and the stuck transaction is older than 2 × `RECEIPT_TIMEOUT`,
send a 0-value self-transfer at the same nonce at `max(1.25 × current, 1.1 × old)` and wait one more window (B's
trigger, A's replacement) — is the right end state but can follow; on Fuji a stuck legacy transaction is a
rare event. B's suggestion of type-2 transactions with a generous `maxFeePerGas` would also stop the tap
overpaying: a legacy `gasPrice` is both fee cap and tip, so the tap always pays the full 1.25× headroom.

**F-4 (Low, but one line) — receipt polling at 10 Hz** — B-2.
Fix (B's): `wait_for_transaction_receipt(h, timeout=RECEIPT_TIMEOUT, poll_latency=2)`. Included in
"before public" only because it is a keyword argument and the server shares its RPC quota with settlement.

### Can wait

**F-5 (Low) — `give()` trusts a balance figure `status()` cached; stale-high lets the mint go and the AVAX
send fail** — A-7 (+ my two-instance path).
Fix (A's): `give()` reads `eth_getBalance` fresh every time and never consults the status cache; the cache
serves `status()` only. Also resolves the dry-tap re-fetch half of B-7 in spirit (one balance call per attempt
is the steady state anyway).

**F-6 (Low) — state file: shape not validated, corrupt file stops the whole server, no runtime reset, temp-file
litter, charge-point `_save()` failure strands the address in memory** — A-5 = B-4, plus B-5.
Fix (merged): in `_load`, validate shape (`isinstance(saved, dict)`, `given` a dict of str→str, `recent` a list
of numbers), catch `(ValueError, TypeError, AttributeError, OSError)` and raise the 503 that names the file
(both); reset `_given`/`_recent` to empty when the file is absent (both); wrap the charge-point `_save()` and
on failure pop the key, drop the slot, raise `TapError(503, "tap state not writable: <file>")` (B-5); add
`"version": 1` on write (B). In `serve_chain.py`, catch `TapError` from `tap_from_env`, start with `tap = None`
and log loudly (A) — the tap is optional and should not take `/infer` down with it. Prune `given` entries older
than ~90 days only if "ever" is not literally required.

**F-7 (Low) — `GET /chain` fails, and costs an RPC call per request, when the balance RPC fails** — A-6 = B-7 (second half).
Fix (merged): wrap the balance read in `status()`, return the last known figure or `null`, and cache the failure
for a few seconds so an outage does not turn every `/chain` into an RPC call that ties up a threadpool thread for
web3's retries.

**F-8 (Low) — precompile recipients pass the EOA check and burn two transactions' gas** — A-4.
Fix (A's): `estimate_gas({"from": tap, "to": to, "value": avax_wei}) != 21_000 → TapError(400)` before
charging; or refuse `int(to, 16) < 2**16` and the Avalanche stateful-precompile ranges.

**F-9 (Low) — `_FileLock` leaks the fd on non-EWOULDBLOCK errors** — B-6.
Fix (B's): `except BlockingIOError: … / except BaseException: os.close(fd); raise`. Apply the same to
`ChainGate._FileLock` in `foliant/chain.py`.

**F-10 (Low) — `_recipient` slices `[2:]` on inputs `is_address` accepts without `0x`** — A-8 = B-8.
Fix (B's): require `address[:2] == "0x"` (simplest and matches the docstring's `0x...`), or normalise first.

**F-11 (Low, hygiene) — unbounded request body, "chain unavailable" wrapping every non-`TapError`, proxy
timeouts, slot kept on a pre-broadcast RPC blip** — A-8.
Fix: check `content-length` and reject > 1 KiB with 413 before `request.json()`; reserve "chain unavailable"
for RPC exception types (F-6 fixes the file cases); document the required reverse-proxy read timeout (a give can
take two receipt windows). The pre-broadcast slot consumption is by design and only needs a docstring line.

## Anything both missed

- **Two-instance path for A-7** (evidence above): the stale-high balance does not need a thread race; a second
  instance's spend is invisible to the first instance's 30 s cache. Same fix as F-5; same Low.
- **B-2's per-give figure**: only one receipt window per give can be spent on a stuck transaction, and only one
  such burst happens before every other visitor is refused pre-send. This lowers B-2, it does not add a finding.
- Legacy `gasPrice` at 1.25× means the tap pays the full headroom as effective price on an EIP-1559 chain
  (fee cap = tip for type-0). A cost observation only, folded into F-3's type-2 suggestion.
- Nothing else with evidence. In particular I looked for and did not find: a way to reach a send without the
  address and slot being persisted first; a lock-ordering problem between `give()` and `status()`; anything
  sensitive in error text or the state file; a nonce path other than the tap key's own.

## Assessment of the two audits

**Audit A** — accurate on every finding (8/8 confirmed; A-7 confirmed by a path A did not test, its own repro
being an injected state rather than the race). Evidence is strong: every claim has a passing test, and the two
`test_cov_*` tests are a genuinely useful observation that the shipped suite's "release" test never reaches the
post-charge release code. Severity calibration is the weak point: A-1 at High overstates the stakes for a
faucet-AVAX demo, though A's instinct that it is the first thing to fix is correct. A's fixes are the more
concrete of the two (env var names, exact guards). A missed B-5 and B-6, both minor. A's "checked and fine"
list is thorough and correct.

**Audit B** — accurate on 7/8; B-2 is right about the mechanism but wrong on the "1,200 per give" arithmetic
and overstates the operational impact, and Medium was a grade too high. B's evidence is at least as strong as
A's (13 tests, an RPC call counter that A lacked, a state table for every exit of `give()` that is correct
line by line and is the single most useful artefact in either report). B's calibration is better than A's on
the drain ("by design, testnet only") but B missed the availability consequence of A-1 entirely — that a
per-hour cap with no per-client dimension is a lock-out, not just a cost — which is the most important thing
either audit found. B's fix for the retry misclassification (decide by the chain) is the one to use. B found
two things A did not (B-5, B-6), both real and both minor.

Neither audit contradicts the other on any fact; they differ only on how much A-1 matters, and I side with A
on ordering and with B on the grade.
