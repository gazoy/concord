# TAP-AUDIT-LOWS — independent audit of `tap-lows` (e13e635, base 5432c95)

Scope: `git diff 5432c95..HEAD` in `/home/claude/wt-low` — `demo/tap.py`, `demo/serve_chain.py`,
`foliant/chain.py`, `tests/test_tap.py` (368 insertions). Brief: TAP-AUDIT-FINAL.md, "Can wait",
F-5..F-11. Reproductions in
`/tmp/claude-0/-home-claude/f52e2597-e0d8-5b12-943c-449c632a675a/scratchpad/lows/`.

## Verdict

Four of the seven findings (F-5, F-7, F-9, F-10) are genuinely closed on every path I could trace, and
the pre-existing invariants all still hold: the charge is still taken before the first broadcast, an
address still gets one attempt ever, the per-client/hourly/daily caps still refuse with zero RPC calls,
two instances over one directory still agree, and the new F-8 probe runs *after* the caps and *before*
the charge, so a refusal genuinely costs the visitor no slot and the tap no gas. But the work is not
finished. The F-6 state-file validation is advertised as "anything that is not the shape we write is a
corrupt file" and is not: `stuck`'s *values* are unchecked, and a `stuck.nonce` that is a string or null
sails through `_load()` and then raises `TypeError` at `demo/tap.py:208` — which, because of declared
deviation (b), now escapes `give()` as an unhandled exception and returns HTTP 500 to every visitor
until an operator edits the file by hand (at 5432c95 the same file produced a clean, if misleading,
502). Deviation (b) is under-scoped in three more places: plain `OSError` from the transport, `ENOLCK`
from `flock` (the exact path F-9 added a re-raise for), and the *unwrapped* success-path `_save()` at
`tap.py:291` — where the visitor has already received 0.02 AVAX and 1000 tokens on chain and is handed a
500 with no transaction hashes. Deviation (a) is correct: I reproduced anvil answering `estimate_gas`
with 21,000 for a value transfer to every precompile 0x01..0x0a, so the `eth_call` really is doing the
work; but the `gas != 21_000` branch is consequently dead on anvil and untested, the probe is skipped
entirely when `FOLIANT_TAP_AVAX=0` (the tap then mints straight to a precompile — reproduced), and it
triples the unlimited-repeat cost of a refusal from 3 to 9 RPC round trips held under both locks. The
F-11 body bound does not do what its docstring says for a chunked body: a 16 MiB body with no
content-length is buffered in full before the 413. Both suites pass, twice each, 29/29 and 219 passed +
1 skipped; the eleven new tests are honest work — I found none passing for the wrong reason, though the
F-9 one accepts either exception type and so silently blesses the 500 above. The reported flake did not
reproduce in 25 targeted runs and 3 full-suite runs across both trees; it is not caused by this change.

## F-5 .. F-11

| Finding | Verdict | Notes |
|---|---|---|
| **F-5** — `give()` trusts `status()`'s balance cache | **CLOSED** | `tap.py:189` calls `self.balance()` directly; `_cached_balance()` has exactly one caller, `status()`. No remaining reader of the cache inside `give()`/`_give_locked`. The regression test genuinely fails against the old code. |
| **F-6** — state file not validated / takes the server down / litter / charge-point save | **PARTLY** | Closed: `isinstance` gate on the object, `given` str→str, `recent` a list, `clients` a dict, late assignment so a bad file never half-replaces good state; absent file resets; `TapError` caught in `make_app` so `/infer` survives; temp-file cleanup works (verified, `repro_misc.py` #1); charge-point rollback correct and verified. **Open: D1** (`stuck` values unchecked → 500) and **D7** (strings-as-numbers, bools, NaN, Infinity, dict `clients` values all accepted). |
| **F-7** — `/chain` 500s and costs an RPC call per request on a balance failure | **CLOSED** | Failure cached for `BALANCE_RETRY`, last-known figure or `null` returned, `give()` unaffected. Test verified sound (the 5-request loop really does make one call). No other consumer of `balanceWei` in the repo, so `null` breaks nothing. |
| **F-8** — precompile recipients burn two transactions' gas | **PARTLY** | The probe works on anvil and runs after the caps and before the charge. But **D5** (skipped when `avax_wei == 0`; reproduced minting 1000 tokens to 0x…01), **D4** (any JSON-RPC error becomes "your address is bad"), **D6** (refusal cost 3→9 RPC calls, unlimited repeats), and the `gas != 21_000` branch is unreachable on anvil and therefore untested; behaviour against Avalanche's stateful precompiles (`0x0200…`), the actual deployment target, is unverified — on anvil those addresses pass the probe cleanly. |
| **F-9** — `_FileLock` leaks the fd on non-`EWOULDBLOCK` errors | **CLOSED** | Correct in both `demo/tap.py:350` and `foliant/chain.py:469`; `except BlockingIOError` is ordered before `except BaseException`; `os.open` failing before the `try` leaves no fd. Verified no leak over 40 iterations. (The re-raise it adds is, however, what feeds **D2**.) |
| **F-10** — `_recipient` slices `[2:]` on inputs `is_address` accepts | **CLOSED** | Prefix required, `0X` normalised, checksum still enforced through a `0X` prefix. No residual mangling path. |
| **F-11** — unbounded body, over-broad "chain unavailable", proxy timeout, slot on a pre-broadcast blip | **PARTLY** | Proxy-timeout docs added; slot-consumption documented as designed; the content-length path is bounded. **Open: D3** (the no-content-length path is fully buffered before the check, contradicting two comments and the module docstring) and **D2** (the narrowed `RPC_ERRORS` leaves four operational conditions as 500s). |

### Exit-state trace of `give()` / `_give_locked()`

| Exit | Memory | Disk | Chain | Reported |
|---|---|---|---|---|
| bad address / bad `client` | untouched | untouched | 0 RPC | 400 ✓ |
| thread lock busy | untouched | untouched | 0 RPC | 503 ✓ |
| flock `EWOULDBLOCK` | untouched | untouched | 0 RPC | 503, fd closed ✓ |
| flock `ENOLCK`, or `os.open` on `.lock` fails | untouched | untouched | 0 RPC | **500** — D2 |
| `_load()` corrupt | untouched (late assignment) | untouched | 0 RPC | 503 ✓ |
| address already given | untouched | untouched | 0 RPC | 429 ✓ |
| per-client / hourly / daily cap | pruned only | untouched | **0 RPC, verified** | 429 ✓ |
| `get_code` says contract | untouched | untouched | 1 RPC | 400 ✓ |
| balance < need | untouched | untouched | 3 RPC | 503 ✓ |
| F-8 probe refuses | untouched | untouched | 9 RPC (D6) | 400 ✓ (D4 mislabels transient errors) |
| stuck-record clearing `_save()` fails (`tap.py:210`) | `_stuck = None` | old record kept | 3–9 RPC | **500**, memory/disk diverge — D2 |
| tx in flight | `_stuck` possibly cleared+saved | consistent | 9+2 RPC | 503 ✓ |
| charge-point `_save()` fails | rolled back exactly | untouched | nothing broadcast | 503 ✓ (verified) |
| `NotBroadcast`, `sent == 0` | address released, slot kept | saved best-effort | nothing in flight | 502 ✓ |
| `NotBroadcast`, `sent == 1` | `failed:` | saved best-effort | mint mined | 502 ✓ |
| `Unconfirmed` | `unconfirmed:<h>`, `stuck` set | saved best-effort | in flight | 504 ✓ |
| other exception, broadcast / not | `failed:` / released | saved best-effort | ambiguous / clean | 502 ✓ |
| success, final `_save()` fails (`tap.py:291`) | `funded` | **`pending`** | **funds delivered** | **500, no tx hashes** — D2 |
| success | `funded` | `funded` | delivered | 200 ✓ |

Only the two unwrapped `_save()` calls (`tap.py:210`, `tap.py:291`) and the lock-acquisition path can
leave memory and disk disagreeing. In the `tap.py:291` case the disk record stays `"pending"`, which
still blocks a second attempt, so *the "never fund twice" invariant is not broken* — but the visitor is
told the give failed when it did not.

## Defects

### D1 (Medium) — `stuck`'s values are not validated; a wrong one 500s every later request
**Location** `demo/tap.py:386` (validation) and `demo/tap.py:208` (crash site).
The check is `isinstance(stuck, dict) and {"nonce","hash","since"} <= set(stuck)` — presence only.
`self._stuck["nonce"] < latest` then compares whatever was in the file against an int.
**Evidence** (`repro_state_shapes.py`, `repro_http_and_work.py`):
```
stuck.nonce is a string   -> !! ESCAPED TypeError: '<' not supported between 'str' and 'int'
stuck.nonce is null       -> !! ESCAPED TypeError: '<' not supported between 'NoneType' and 'int'
POST /tap with stuck.nonce="7": HTTP 500  body='Internal Server Error'
GET /chain with the same file: 200        # the operator sees nothing wrong
```
`stuck.since` as a string is the same class, crashing at `int(self._stuck['since'])` (`tap.py:216`) on
the in-flight refusal path. The tap never *writes* a bad `stuck` itself, so this needs a corrupted or
hand-edited file — which is precisely F-6's threat model. At 5432c95 it was a clean 502.
**Fix** In `_load`, require `isinstance(stuck["nonce"], int) and not isinstance(stuck["nonce"], bool)`,
`isinstance(stuck["hash"], str)`, `isinstance(stuck["since"], (int, float))`; add those three shapes to
`test_bad_state_file_fails_closed`'s table.

### D2 (Medium) — deviation (b) leaves four operational conditions as HTTP 500
**Location** `demo/tap.py:163` (`RPC_ERRORS`), `tap.py:210` and `tap.py:291` (unwrapped `_save()`),
`tap.py:343` (`os.open` outside the `try`).
**Evidence** (`repro_oserror_escapes.py`):
```
ENOLCK from flock (F-9 re-raise)   -> !! ESCAPED OSError: [Errno 37] No locks available (HTTP 500)
final _save() ENOSPC after funding -> !! ESCAPED OSError: [Errno 28] No space left on device (HTTP 500)
       visitor actually received: 20000000000000000 wei, 1000000000 tokens
       on-disk record now: {"version": 1, "given": {"0xf186…": "pending"}, …}
       in-memory record now: {'0xf186…': 'funded'}
bare OSError from an RPC call      -> !! ESCAPED OSError: broken pipe on the socket (HTTP 500)
state directory removed under us   -> !! ESCAPED FileNotFoundError: … (HTTP 500)
ConnectionError from an RPC call   -> clean TapError 502: chain unavailable: ConnectionError   # ok
```
All four were clean 502s at 5432c95. The middle one is the worst: the give *succeeded*, the visitor is
funded, and they get an opaque 500 and lose the transaction hashes.
**Fix** Keep the narrowed `RPC_ERRORS` for the "chain unavailable" *message* — that part of the
deviation is right — but add a final `except Exception` in `give()` mapping to
`TapError(503, "the tap is temporarily unavailable")` so nothing reaches the visitor as a traceback, and
wrap `tap.py:210` and `tap.py:291` in `try/except OSError: pass` like the other three `_save()` calls.

### D3 (Low) — the 1 KiB body bound does not hold for a body with no content-length
**Location** `demo/serve_chain.py:15-18` (module docstring), `:124-131` (comment and code).
`raw = await request.body()` buffers the entire stream before `len(raw)` is looked at, so both
"refused 413 here, before they are read" and "refuse anything larger before reading or parsing it,
rather than buffering whatever a visitor sends" are false for exactly the case the second check exists
for.
**Evidence** (`repro_work2.py`): `chunked 16777216 B body -> HTTP 413; bytes Request.body() buffered
before the check: [16777216]`.
**Fix** Iterate `request.stream()` and abort once the accumulated length passes `MAX_TAP_BODY`;
otherwise correct both comments to say the bound only applies to a declared content-length. Either way
add a test for the chunked path — there is none.

### D4 (Low) — a transient node error in the F-8 probe is reported as "your address is bad"
**Location** `demo/tap.py:200`. `except (ContractCustomError, ContractLogicError, Web3RPCError)` catches
every JSON-RPC error, including `-32603 internal error` (which is what anvil returns for the precompile
case, so it cannot simply be excluded) and a pruned-state or restarting node.
**Evidence** (`repro_misc.py` #3): a freshly created EOA, with `eth_call` made to raise
`Web3RPCError({"code": -32603, "message": "missing trie node / node restarting"})`, is refused
`400: the address cannot receive a plain transfer (a precompile?)`. Nothing is charged, so it is
recoverable, but the visitor is told their address is wrong when the node is.
**Fix** Match on the revert/OOG shape (anvil's `PrecompileOOG`, geth's `execution reverted` /
`out of gas`) and let anything else fall through to the 502.

### D5 (Low) — F-8 is bypassed entirely when `FOLIANT_TAP_AVAX=0`
**Location** `demo/tap.py:191` (`if self.avax_wei:`).
**Evidence** (`repro_misc.py` #2): with `avax_wei=0`, `give("0x…01")` succeeds and
`tokens now stranded at the precompile: 1000000000`.
**Fix** Run the recipient probe unconditionally (probe with `value: 0` when `avax_wei` is 0), or gate the
mint on it as well.

### D6 (Low) — F-8 triples the cost of an unlimited-repeat refusal, under both locks
**Location** `demo/tap.py:196-205`.
**Evidence** (`repro_work2.py`):
```
refusal cost, precompile 0x01 (F-8) : 400     9 rpc calls  [getCode, gasPrice, getBalance, estimateGas, chainId×3, call]
refusal cost, a contract            : 400     1 rpc calls
refusal cost, per-client cap hit    : 429     0 rpc calls   # the cap path is still free, good
```
A refusal past the caps consumes no slot, so one client can repeat it without limit; each repeat now
costs 9 RPC round trips and holds `self._lock` *and* the flock for their duration, serialising the tap.
Pre-existing at 3 calls; this change makes it 3× worse and 3× longer.
**Fix** Count refusals past the caps against the per-client budget (a separate, larger counter), or move
the probe to just after the charge and release the address on a probe refusal the way `NotBroadcast`
with `sent == 0` already does.

### D7 (Low) — `_load()` accepts shapes its docstring says it rejects
**Location** `demo/tap.py:364-394`. **Evidence** (`repro_state_shapes.py`): `"recent": ["1","2"]` loads
as `[1.0, 2.0]`; `[true, false]` as `[1.0, 0.0]`; `NaN` loads and is silently pruned;
`"clients": {"h": {"1": 2}}` loads as `{"h": [1.0]}`; duplicate JSON keys silently take the last.
`Infinity` is the one with teeth: `recent=[inf,inf,inf]` never expires, so the tap is permanently 429 —
fail-closed, but unrecoverable without editing the file. The docstring's "Anything that is not the shape
we write is a corrupt file" overstates the code.
**Fix** `isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)` for every
timestamp, `isinstance(ts, list)` for each `clients` value; or soften the docstring to match.

### D8 (Nit) — the F-9 test blesses the 500 of D2
**Location** `tests/test_tap.py:588`, `with pytest.raises((OSError, TapError))`. The comment says "how it
is reported is not the point", but it is: that tuple is the only place the suite touches the ENOLCK
path, and it accepts the unhandled `OSError`. Tighten it to the single expected type once D2 is decided.

### D9 (Nit, pre-existing) — `_save()` does not fsync the directory after `os.replace`
**Location** `demo/tap.py:404`. The content swap is atomic (so the "upgrade write" for an old-format
file is safe against a crash in the sense the brief asked about — you get either the old file or the new
one, never a half-written one), but the rename itself is not made durable, so a power loss can lose the
last write and resurrect an address as unfunded. Unchanged by this commit; noted for completeness.

**Also noted, out of scope:** `ChainGate._load` (`foliant/chain.py:446`) still has no shape validation
and lets a raw `ValueError` out of `ChainGate.__init__`. The tap and the gate are now asymmetric. The
brief scoped F-6 to the tap, so this is not a defect of this change.

## The two declared deviations

**(a) `eth_call` added alongside `estimate_gas` for F-8 — CORRECT, and I verified the claim myself.**
`probe_anvil_precompile.py` against a fresh anvil:
```
0x01 ecrecover  code=0  estimate_gas=21000  eth_call=RAISED Web3RPCError {'code': -32603, 'message': 'EVM error PrecompileOOG'}
0x02 sha256     code=0  estimate_gas=21000  eth_call=RAISED … PrecompileOOG
0x04 identity   code=0  estimate_gas=21000  eth_call=RAISED … PrecompileOOG
0x08 ecPairing  code=0  estimate_gas=21000  eth_call=RAISED … PrecompileOOG
0x0a pointeval  code=0  estimate_gas=21000  eth_call=RAISED … PrecompileOOG
plain EOA       code=0  estimate_gas=21000  eth_call=ok
```
The implementer's statement is exactly right: anvil answers 21,000 without simulating, so the
`gas != 21_000` branch never fires there and the `eth_call` is the whole fix. The deviation was
necessary and is well judged. Three caveats the implementer should have recorded with it: the
`gas != 21_000` branch is now dead code on the only chain the suite runs against, and therefore
untested (D6's test-coverage half); the probe's error classification is too broad (D4); and it is
unverified against the deployment target — the same script shows `0x0200000000000000000000000000000000000000`
and `…0002`, the Avalanche stateful-precompile addresses the brief's alternative fix named explicitly,
passing cleanly on anvil, so on Fuji/subnet-EVM this fix may or may not catch them. The brief offered
"or refuse `int(to, 16) < 2**16` and the Avalanche stateful-precompile ranges" as a second half; taking
only the RPC route leaves that unresolved. **Accept the deviation, add the cheap address-range check as
belt and braces.**

**(b) only `Web3Exception, requests.RequestException, ConnectionError, TimeoutError` count as RPC
failures — RIGHT IN PRINCIPLE, WRONG AS SHIPPED.** The principle is sound: a `TypeError` of ours is not
"chain unavailable", and the old blanket `except Exception` hid real bugs behind a plausible-looking
502. The type set itself is well chosen (`Web3RPCError`, `ContractLogicError` and `TimeExhausted` are
all `Web3Exception` subclasses, and `requests.ConnectionError` is a `RequestException`, so the four
cover the transport). What is wrong is that nothing was put in the blanket's place. Yes — things now
reach the visitor as a 500 that should not: the four conditions in **D2**, all of them ordinary
operational failures rather than bugs (a full disk, a removed state directory, `ENOLCK` on an NFS mount,
a broken socket), plus **D1**'s corrupt-file `TypeError`, which turns a recoverable bad file into a
permanent 500 on `POST /tap` while `GET /chain` keeps returning 200 so nothing looks wrong. A narrowed
`RPC_ERRORS` needs a catch-all *below* it that produces a clean 503 and logs the traceback server-side —
that gets the diagnostic benefit the deviation was after without handing visitors tracebacks.
**Accept the narrowing, require the catch-all.**

## Test quality

Eleven new tests, 226 added lines; base 208 passed → branch 219 passed, so exactly 11 added and none
removed or weakened. I read each one against its name and docstring.

Sound, and each genuinely fails against the pre-change code:
- `test_stale_balance_cache_cannot_mislead_give` — the `reads` spy is decisive: nothing else in the give
  path calls `get_balance`, so a non-empty `reads` can only mean `give()` asked the chain. Against the
  old `_cached_balance(30)` it would be empty.
- `test_bad_state_file_fails_closed` — all ten shapes really are rejected for the reason claimed, and it
  checks both a fresh instance *and* a running one, which is the part that matters.
- `test_absent_state_file_resets_the_tap`, `test_old_format_state_file_loads`,
  `test_unwritable_state_at_the_charge_point_releases_the_address`, `test_make_app_survives_a_broken_tap`,
  `test_chain_endpoint_survives_a_balance_rpc_failure` (I re-derived the `len(calls) == 1` arithmetic;
  it is right, and the `cold` tap really does exercise the never-read `None` case),
  `test_address_prefix_is_required_and_normalised`, `test_oversized_body_refused`.
- `test_precompile_recipient_refused_before_anything_is_charged` — passes for a *real* reason, but not
  the one a reader would assume: both refusal branches contain the string "plain transfer", so the
  assertion cannot tell the `eth_call` path from the `gas != 21_000` path, and on anvil it is always the
  former. Worth splitting the two messages so the test says which fired.

One weak test: `test_file_lock_does_not_leak_a_descriptor` (**D8**) — the fd-count assertion is real and
would catch the old leak (40 iterations vs a `<= 2` tolerance), but `pytest.raises((OSError, TapError))`
accepts the unhandled `OSError` that becomes a 500.

Paths with no test at all:
1. the chunked/no-content-length body bound (D3) — the one case the second length check exists for;
2. the `gas != 21_000` branch of F-8 — unreachable on anvil;
3. F-8 with `avax_wei == 0` (D5);
4. F-8 against a transient RPC error (D4);
5. the narrowing itself — nothing asserts that a `Web3Exception` still yields 502 and that a non-RPC
   error yields *something clean*; had there been such a test, D2 would have been caught;
6. `_save()`'s new `except BaseException`/unlink — `test_unwritable_…` replaces `_save` wholesale, so
   the litter cleanup is never executed by the suite (it does work: `repro_misc.py` #1 leaves only the
   `.lock` file behind);
7. the accepted-but-wrong shapes of D1 and D7.

## The reported flake

`tests/test_chain_audit.py::test_v3_two_processes_share_state_exactly_once` **did not reproduce, and is
not caused by this change.** Evidence:

| where | runs | result |
|---|---|---|
| `/home/claude/wt-low` (e13e635), the test alone | 12 | 12 passed |
| `/home/claude/wt-low`, whole `test_chain_audit.py` | 5 | 5 × `36 passed, 1 skipped` |
| `/home/claude/wt-low`, `pytest tests -q` | 2 | 2 × `219 passed, 1 skipped` |
| `/home/claude/concord` (5432c95), the test alone | 8 | 8 passed |
| `/home/claude/concord`, `pytest tests -q` | 1 | `208 passed, 1 skipped` |

The only change this branch makes to `foliant/chain.py` is an `except BaseException` guard around
`fcntl.flock` in `ChainGate._FileLock.__enter__`. That guard is on the error path; `flock(LOCK_EX)`
blocks rather than failing under contention, which is exactly what this test produces, so it cannot
change the outcome. The test does fork two processes onto a shared anvil and a shared state directory,
so a genuinely loaded machine could time out on the RPC — that would be a pre-existing environmental
flake, not a regression. **No action on this branch; if it recurs, instrument `_proc_accept`'s RPC
errors rather than the lock.**

## Recommendation

**Merge after fixing 3: D1, D2, D3.**

D1 and D2 are the same wound from two sides — the validation lets a bad shape through and the narrowed
exception set no longer catches what it produces — and between them they turn a hand-recoverable state
file, a full disk and a failed `flock` into tracebacks for visitors, with the `tap.py:291` case handing a
500 to someone who *was* funded. Both are small, local fixes: three `isinstance` checks, a catch-all
below `RPC_ERRORS`, and `try/except OSError` around the two unwrapped `_save()` calls. D3 is a false
claim in a shipped docstring and is either a five-line streaming loop or a two-line correction.

D4–D9 are fine as follow-ups; none of them can lose money or double-fund an address. I would also ask
for the two F-8 messages to be distinguishable and for a test asserting that a non-RPC failure inside
`give()` produces a clean status — that single test is what would have caught D2.

---

# Round 2 — b67ec50 on top of e13e635

Re-audited `git diff e13e635..HEAD` (242 insertions across `demo/tap.py`, `demo/serve_chain.py`,
`tests/test_tap.py`; nothing under `foliant/` is touched this round). Worktree left unmodified, nothing
committed. New reproduction: `scratchpad/lows/r2_checks.py`; the e13e635 source export used to
falsify the new tests is at `scratchpad/lows/old/`.

## Per-defect verdict

| # | Round 1 | Now | Evidence |
|---|---|---|---|
| **D1** — `stuck` values unvalidated → 500 | Medium | **CLOSED** | `_is_int` (rejects `bool`), `isinstance(hash, str)`, `_is_number` (rejects `bool`, requires `math.isfinite`). All six shapes I reproduced are now rejected at load, and the extended `test_bad_state_file_fails_closed` table covers exactly them. Extending `since` to `isfinite` was the right call and was not in my write-up: `Infinity` would have survived the type check and then blown up at `int(since)` on `tap.py:216`. |
| **D2** — four conditions reaching the visitor as 500 | Medium | **CLOSED** | Verified all four: `ENOLCK → TapError 503`, `state dir removed → TapError 503`, `bare OSError from an RPC call → TapError 503`, and the traceback really does reach stderr. The final `_save()` now swallows `OSError` and returns the hashes (`test_a_failed_final_save_still_reports_the_funding` confirms the visitor keeps 0.02 AVAX + 1000 tokens and that the on-disk `"pending"` still refuses a second attempt). Moving `os.open` inside the guarded region with `_close()` is correct — `_close()` is null-safe, so an `os.open` failure closes nothing and re-raises cleanly. |
| **D3** — 1 KiB bound did not hold without content-length | Low | **CLOSED** | Measured, see below. |
| **D4** — transient node error reported as a bad address | Low | **PARTLY** | The half that mattered is fixed; the text list is still prose-matchable. See below. |
| **D5** — F-8 skipped when `avax_wei == 0` | Low | **CLOSED** | Probe unconditional. I confirmed independently that `eth_call` with `value: 0` to `0x…01` still raises `PrecompileOOG`, so the probe works at zero value and the fix is not merely nominal. |
| **D6** — refusal cost 3→9 RPC calls, repeatable for free | Low | **CLOSED** | The range check precedes `get_code`, so a reserved recipient costs **zero** RPC calls; the rewritten test enforces this with a `no_rpc` guard on `get_code`. The only remaining unlimited free vector is a contract address at 1 RPC call, which is the pre-existing level. |
| **D7** — `recent`/`clients` accept strings, bools, NaN, Infinity | Low | **OPEN** | Not claimed, not fixed, and now *inconsistent*: `since` gained `math.isfinite`, the timestamps in `recent` and `clients` did not. Re-verified: `recent: [Infinity]` loads and permanently occupies an hourly slot; `clients: {"h": [Infinity]}` locks that one client out forever; `NaN` is silently pruned; `["1","2"]` and `[true]` are coerced. All fail-closed. |
| **D8** — the F-9 test blessed the 500 | Nit | **CLOSED** | Now `pytest.raises(TapError)` plus `assert e.value.status == 503`. |
| **D9** — no directory fsync after `os.replace` | Nit | **OPEN** | Unchanged, pre-existing, still fine to defer. |

## The four things you asked me to check hardest

**(1) The catch-all cannot swallow what should propagate — correct, with one structural gap.**
Clause order is `TapError` → `RPC_ERRORS` → `Exception`, so `Unconfirmed` still surfaces as its 504 and
a `Web3RPCError` still says "chain unavailable". `except Exception` does not catch `BaseException`, and
I confirmed a `KeyboardInterrupt` raised inside `give()` propagates untouched (`asyncio.CancelledError`
is likewise a `BaseException` in 3.8+, so a client disconnect is not swallowed either). The gap:
`_recipient()` and the `client` check run **before** `self._lock.acquire()` and therefore outside the
`try`, so a non-`TapError` from there still escapes. I demonstrated it with a contrived `str` subclass
whose `__getitem__` raises (`!! ESCAPED RuntimeError`), and I could not construct a realistic trigger —
`Web3.is_address` on a `str` always returns a bool and `to_checksum_address` only runs on input it has
already validated. So: the claim "every exit of `give()` yields a `TapError`" is true in practice and
not quite true structurally. One-line fix if you want it literally true: move the two pre-checks inside
the `try`. Not a merge blocker.
Secondary note: the catch-all also converts an `AssertionError` from the tests' `no_rpc` guards into a
`TapError(503)`. Every such guard in the suite asserts a specific status (429/400), not merely the
exception type, so none of them is weakened — but a future guard that only checks `pytest.raises(TapError)`
would be silently useless.

**(2) The streaming bound does bound.** `r2_checks.py` drives the ASGI app directly and counts both the
chunks pulled and the bytes read:

| offered | result | chunks pulled | bytes actually read |
|---|---|---|---|
| 512 KiB in 1 KiB chunks | 413 | 2 | 2,048 |
| 8 MiB in 64 KiB chunks | 413 | 1 | 65,536 |
| 64 MiB in 1 MiB chunks | 413 | 1 | 1,048,576 |
| 4 KiB in 1-byte chunks | 413 | 1,025 | 1,025 |
| 4 MiB behind a `content-length: 10` | 413 | 1 | 65,536 |
| slow-loris, 1 byte every 1 ms | 413 | 1,025 | 1,025 in 1.21 s |
| honest `{"address": …}` | 200 | 1 | 57 |

The bound is therefore `MAX_TAP_BODY + one transport chunk`, not `≤ MAX_TAP_BODY` — the last chunk is
appended before the length is tested. Behind uvicorn/h11 the chunk is the socket read size (~64 KiB), so
the real worst case is ~65 KiB against the old code's unbounded full buffer. That is a correct and
sufficient fix and the rewritten docstring ("read a chunk at a time and abandoned there, so a body with
no content-length is not buffered in full either") is now accurate. The lying `content-length` is
handled — the short-circuit only ever *adds* a rejection, so a liar falls through to the stream check
and is still refused. The slow-loris holds one event-loop coroutine, never a threadpool thread, and
never reaches `give()` or either lock; that is unchanged from before and is the transport's timeout to
set, not the tap's.

**(3) The D4 text matching is too broad, in exactly the way you suspected.** Against
`PROBE_REFUSALS = ("precompile", "execution reverted", "out of gas", "invalid opcode", "revert")`:

| node message | classified as | via |
|---|---|---|
| `EVM error PrecompileOOG` (anvil) | 400 bad address ✓ | `precompile` |
| `execution reverted` (geth/erigon), `Execution reverted` (besu) | 400 ✓ | `execution reverted` |
| `EVM error Revert` (reth) | 400 ✓ | `revert` *(only the bare entry catches this one)* |
| `out of gas`, `invalid opcode: INVALID` | 400 ✓ | as named |
| `missing trie node; node restarting` | 502 ✓ | — (**the D4 regression is gone**) |
| `daily request count exceeded` | 502 ✓ | — |
| `insufficient funds for gas * price + value` | 502 ✓ | — |
| `node is reverting to the last snapshot` | **400 ✗** | `revert` |
| `the block ran out of gas room` | **400 ✗** | `out of gas` |
| `cannot deploy: not authorized` (a subnet-EVM stateful precompile) | **502** (fail-safe ✗) | — |

So the important half is genuinely fixed and the residue is small: two plausible node-side prose strings
still tell a visitor their good address is bad, and one plausible stateful-precompile refusal comes back
as "chain unavailable" instead of a clean 400. Neither charges anything, neither funds anything, and the
address stays eligible in both directions, so this is cosmetic-plus. Note the bare `"revert"` entry
cannot simply be deleted — reth's `EVM error Revert` needs it. Suggested follow-up: replace `"revert"`
with `"evm error revert"` and `"out of gas"` with `"err: out of gas"`/`"out of gas:"`, or key off the
JSON-RPC error code (3 and -32000 for reverts) rather than prose.

**(4) The reserved ranges are right.** `((0, 2**16), (1 << 152, (1 << 152) + 256), (2 << 152, (2 << 152) + 256))`
expands to exactly:

```
0x0000000000000000000000000000000000000000 .. 0x000000000000000000000000000000000000ffff   (65536)
0x0100000000000000000000000000000000000000 .. 0x01000000000000000000000000000000000000ff     (256)
0x0200000000000000000000000000000000000000 .. 0x02000000000000000000000000000000000000ff     (256)
```

`1 << 152` is `0x01` in the top byte of a 20-byte address, so the second and third ranges are the low
256 of Avalanche's two reserved prefixes — covering the C-Chain native-asset precompiles at
`0x0100…0010`/`…0011` and the subnet-EVM stateful precompiles at `0x0200…0000`–`…0005`, which was the
brief's alternative fix for F-8 and the residual risk I flagged in round 1. Boundaries are exclusive and
correct: `0x0000…010000`, `0x0100…0100`, `0x0200…0100` and `0x0300…0000` are all *not* reserved, so
nothing beyond the intended prefixes is caught. 66,048 addresses out of 2^160, i.e. 4.5e-44 of the
space — a keccak-derived EOA will never land there. `int(to, 16)` is safe because `_reserved` only ever
sees a checksummed address `_recipient` already validated.

**(5) The eight tests are honest.** I exported the e13e635 source to a clean tree, dropped the new
`tests/test_tap.py` onto it and ran all eight there — **8 failed, 26 deselected**, each for the right
reason. The most useful one: `test_chunked_oversized_body_is_not_buffered` fails against the old code
with `AssertionError: 512` (the app pulled all 512 chunks before refusing), which is the D3 defect
reproduced from inside the suite. `test_a_non_rpc_failure_is_a_clean_status` covers the gap I said would
have caught D2, over both the library and the HTTP surface. `test_probe_tells_a_bad_recipient_from_a_bad_node`
exercises both sides of the D4 branch. Two honest-but-weak spots, neither serious: in
`test_tokens_only_tap_still_probes_the_recipient` the first assertion uses `0x…01`, which the *range*
check refuses before the probe is reached, so only the second half (the `eth_call` spy asserting a
`gas == 21_000` call at zero value) actually tests what the name claims; and
`test_precompile_recipient_refused_before_anything_is_charged` no longer covers the `eth_call` probe at
all, since all four of its addresses are now caught by the range check — the probe's precompile path has
moved entirely into `test_probe_tells_a_bad_recipient_from_a_bad_node`, which fakes the error rather
than using a real one. The `gas != 21_000` branch remains dead on anvil and untested, as in round 1.

**(6) D1's validation is complete for `stuck`.** All three values are now typed, `bool` is excluded from
both numeric checks (`True` is an `int` in Python, and `_is_int` rejects it — the test table includes
`"nonce": true`), and `since` is `isfinite`. `nonce` may still be negative, which is harmless for the
`< latest` comparison. No other shape escapes *through `stuck`*. What still escapes is D7's territory —
the `recent` and `clients` timestamps, which get `float()` and no finiteness check — and that
inconsistency is now more conspicuous, since the same commit added `isfinite` one line above.

## Test runs

| suite | runs | result |
|---|---|---|
| `PYTHONPATH=. pytest tests/test_tap.py -q` | 2 | 34 passed (76.7 s, 72.9 s) — was 29 at e13e635 |
| `pytest tests -q` | 5 | 224 passed, 1 skipped every time (106–117 s) — was 219 |
| new tests against e13e635 source | 1 | 8 failed, 26 deselected |

## The flake

**Consistent with my evidence, and it does not block this merge.** There is no conflict between the two
observations: my 25 clean runs were of *one test in isolation*, which is precisely the configuration the
implementer says passes. I have now completed eight full-suite runs across the two rounds (two at
e13e635, five at b67ec50, one at 5432c95) with zero failures; at a 1-in-4 rate that outcome has
probability ≈ 0.10, which is unremarkable for a load-dependent flake on a differently-loaded machine —
my runs finished in 89–117 s, so this box may simply not be pushing it hard enough.

Pre-existing: yes, and I can be specific. `chain.py:282` is
`raise FoliantError(f"transaction reverted: {h.hex()}")` inside `ChainAgent._send`, which builds its
nonce from `get_transaction_count(address, "pending")` and waits for a receipt. Nothing on that path is
touched by either commit: round 2 changes no file under `foliant/` at all, and round 1's only change
there is an `except BaseException` guard on `ChainGate._FileLock.__enter__` — a different class, and an
error path that cannot fire when `flock` is blocking on contention. Two different tests failing the same
way, only under a full run, on a receipt status rather than an estimateGas revert, points at shared
accounts and wall-clock-sensitive contract state (channel/pool timeouts elapsing while a transaction
waits for inclusion under load), which is a property of the test fixtures, not of this branch.

**Recommendation on the flake: track it separately, do not gate this merge on it.** Worth doing when
someone picks it up: have `ChainAgent._send` include the revert reason and the block/nonce in the
`FoliantError`, and re-run the suite under `-p no:randomly -x` with `--timeout` to catch it with state
intact. Do not "fix" it by widening timeouts until the cause is known.

## Final recommendation

**Merge.**

All three blockers from round 1 (D1, D2, D3) are closed, and I verified each one by reproduction rather
than by reading the diff: the corrupt-`stuck` 500 is gone, every realistic exit of `give()` now yields a
`TapError` with the traceback going to the operator instead of the visitor, and the body bound holds for
chunked, lying-content-length and slow-loris inputs. D5, D6 and D8 are closed as well, and the
reserved-range check the implementer added on their own initiative resolves the residual F-8 risk I
raised about Avalanche's stateful precompiles — the arithmetic is exactly right and it makes the
cheapest abuse vector cost nothing at all. Both suites are green, five full runs, and all eight new or
tightened tests genuinely fail against the previous commit.

What is left is small and none of it can lose money, double-fund an address, or hand a visitor a
traceback. For a follow-up, in the order I would do them: **D4's residue** (drop bare `"revert"` in
favour of `"evm error revert"`, anchor `"out of gas"`, or key off the JSON-RPC error code) — two
plausible node messages currently blame the visitor for the node's fault; **D7** (`math.isfinite` on the
`recent` and `clients` timestamps, for consistency with the `since` check added in this very commit) —
an `Infinity` in either still means a permanent, silent 429; **the `_recipient` pre-check** moved inside
the guarded region, so "every exit is a `TapError`" is structurally and not merely practically true; and
**D9**'s directory fsync. None is worth another round.
