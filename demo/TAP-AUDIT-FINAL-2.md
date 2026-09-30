# TAP-AUDIT-FINAL-2 — audit of the two blind implementations of F-1..F-4

Scope: branches `tap-fix-x` (worktree `/home/claude/wt-x`, commit bfb850b) and `tap-fix-y` (`/home/claude/wt-y`,
3a1338d), both from base d9c7d03, each fixing F-1..F-4 of `demo/TAP-AUDIT-FINAL.md`. I read both diffs in full
(`demo/tap.py`, `demo/serve_chain.py`, `tests/test_tap.py`; Y also commits `demo/TAP-AUDIT-FINAL.md`, byte-identical
to the copy in the main tree), ran both suites (X: `tests/test_tap.py` 19 passed, `tests` 209 passed 1 skipped;
Y: `tests/test_tap.py` 18 passed, `tests` 208 passed 1 skipped on one run and 1 failed on another — see Y-1), and
wrote nine checks of my own in `scratchpad/final2/test_final2.py`, run against each worktree in turn
(X: 6 passed / 3 failed; Y: 7 passed / 2 failed; every failure is explained below and none is a false alarm).
I verified the two library facts the findings rest on against the installed sources: web3 7.16 caches
`provider.make_request` inside `provider.request_func()` (`providers/base.py:107-130`), and Starlette's
`Headers.__getitem__` returns the *first* header line of a repeated name.

## Verdict

**Adopt Y (`tap-fix-y`)**, with one test fix (Y-1) and one two-line hardening in `serve_chain.py` that both
branches need (XY-1), and take nothing from X's code. The two implementations are close to identical in design —
same per-client and daily caps keyed by a sha256 of the client, same `FOLIANT_TRUST_PROXY` / last-entry
`X-Forwarded-For` rule, same chain-decided classification in `_send`, same `Unconfirmed` subclass and persisted
`stuck` record, same `poll_latency=2` — and both close all four findings on the production path. Y is preferred
on correctness of detail: its `_send` signs first and knows the transaction hash before broadcasting, so the F-2
`failed:` record and 502 name the hash the operator needs to reconcile (X's record carries only the node's error
text, no hash); its stuck-record clearing spells out all three exits (mined, replaced, dropped) in one condition;
its F-2 test provokes the node's genuine "already known"/"nonce too low" error by sending the raw transaction
twice, where X raises a synthetic error; and its per-client no-RPC check is real, whereas X's
`test_per_client_cap_checked_before_any_rpc` is vacuous — it spies on `provider.make_request`, which web3 never
calls after construction, so `calls == []` would hold even if the tap made every RPC call in the book. Y's one
defect is a flaky test (it lowers `RECEIPT_TIMEOUT` to 1 s without lowering `RECEIPT_POLL` from 2 s, so a
healthy give can time out before its second poll; failed 2 of 3 isolated runs and 1 of 2 full runs); the fix is
one line and I verified it (4/4 passes). Y's state layout is also the smaller change (it keeps one timestamp
list and slices the hour out of the day, rather than X's third list), and it commits the audit it implements,
matching the precedent of `TAP-REVIEW.md` being tracked.

## Per-finding table

| | X (`tap-fix-x`) | Y (`tap-fix-y`) |
|---|---|---|
| **F-1** per-client limit | **CLOSED** on the code path. Per-client/day (sha256 of client) and global/day caps, checked before `get_code`, charged with the hourly slot, persisted and reloaded under the file lock. Test for the no-RPC property is vacuous (X-1). Shares XY-1 (multi-line XFF) and XY-2 (hop-count docs). | **CLOSED**. Same design; no-RPC property tested validly (patches `w3.eth.get_code` to raise; instance patching of `eth` methods is proven effective by the pre-existing `test_failure_before_broadcast_releases_the_address`). Shares XY-1, XY-2. |
| **F-2** retry misclassified | **CLOSED** for the release bug; the record lacks the hash (X-2). When the chain check itself fails the address is spent (fail-closed, verified). | **CLOSED**. Hash computed from `signed.hash` before send, so the 502 and the `failed:` record name it. Same fail-closed behaviour when the check fails (verified). |
| **F-3** stuck tx | **CLOSED**. `unconfirmed:<hash>` record, `stuck {nonce,hash,since}` persisted, 503 names hash/nonce/since, cleared on mined / replaced / dropped (all three verified with anvil), address stays spent, operator recipe in docstring. | **CLOSED**. Same; clearing condition `nonce < latest or pending == latest` is the more explicit of the two. Test is flaky (Y-1). |
| **F-4** poll latency | **CLOSED**. `RECEIPT_POLL = 2` passed on both waits; tested via kwarg spy. | **CLOSED**. Same. |

## Adversarial checks (what I tried, and the answer for both)

- **Exhaust the tap for others from one client.** A single IP now gets 3 attempts/day; the hourly cap still
  applies on top; a visitor rotating IPs (an IPv6 /64 makes that free) is bounded by `FOLIANT_TAP_PER_DAY=100`,
  after which *everyone* is locked out until the day rolls — the adjudicated design, and a conscious trade
  (a rotating attacker now buys a day-long lock-out for 100 addresses instead of an hourly one for 20). Not a
  defect against F-1's stated goal ("raise the cost of the one-line loop"); it is worth a line in the docstring
  and the operator can lower `FOLIANT_TAP_PER_HOUR`. Neither branch keys IPv6 by prefix; optional.
- **Forge the client key with `X-Forwarded-For`.** With `FOLIANT_TRUST_PROXY` unset the header is ignored
  (tested in both). With it set, both take the *last comma-separated entry of the first header line*.
  Behind one nginx using `$proxy_add_x_forwarded_for` (merges repeated lines, appends `$remote_addr`) or
  `$remote_addr` (overwrites), the last entry is the peer nginx saw: **correct**. Behind zero proxies with
  the flag set, every entry is the visitor's: fully forgeable — an operator misconfiguration both branches
  leave implicit (XY-2). Behind two appending proxies (CDN → nginx), the last entry is the CDN's egress IP,
  so all visitors share a handful of keys and the per-client cap becomes a global lock-out at 3/day (XY-2).
  Behind a proxy that adds `X-Forwarded-For` as a **separate header line** rather than appending (HAProxy's
  `option forwardfor` does), Starlette's `headers.get` returns the visitor's forged first line, and the
  per-client cap is bypassed — reproduced on both branches (`test_xff_multiple_header_lines`: second request
  from the same proxy peer with a different forged line is served, 200 instead of 429). Fix in XY-1.
- **F-2 when `get_transaction_count` itself fails.** The `ConnectionError` escapes `_send` as a non-RPC
  exception, `broadcast` is already `True`, so the generic handler records `failed:funding failed:
  ConnectionError` and the address is spent; the mint is on chain. Fail-closed on both (verified). The node's
  original error text is lost from the record; cosmetic.
- **F-2 false negative.** If a load-balanced public RPC answers the retry from a node whose pending count
  lags, the check says "not in pool" and the address is released while the transaction sits in another
  node's pool. Residual risk the adjudication accepted; not a branch defect. The optional belt-and-braces
  (disable web3's retry of `eth_sendRawTransaction` on the tap's own provider) is still available.
- **F-3 exits.** Every exit of `_give_locked` writes the right state on both branches: success → `funded`,
  `stuck` untouched (it was cleared before charging); `NotBroadcast` → released or `failed:` as before;
  `Unconfirmed` → `unconfirmed:<hash>` + `stuck` + save; any other → `failed:`/released as before. The stuck
  record clears on the next attempt when the transaction was mined, replaced by the operator's self-transfer
  (verified with a 1.2× same-nonce replacement), or dropped from the pool (verified with
  `anvil_dropTransaction`; the freed nonce is reused). The `unconfirmed:` visitor stays spent in all three
  cases, as specified. The record survives a *dry* refusal (which returns before the nonce check) — harmless,
  since it is only named when something is actually in flight (X guards with `nonce >= latest`; Y clears
  first). The 503 leaks hash, nonce and a timestamp — all public on chain; nothing else.
- **F-2 path leaves a transaction in flight un-waited.** Both raise the 502 immediately, so the next visitor
  inside ~one block gets the *unnamed* in-flight 503 (no `stuck` record is made). Acceptable and as adjudicated;
  Y's record has the hash, so the operator can still find it. Optional improvement noted in the merge plan.

## Invariants (pre-existing) — both branches

| Invariant | X | Y | Evidence |
|---|---|---|---|
| Charge (address + hourly + daily + client) persisted before any broadcast | ✔ | ✔ | Same `_save()` before the `try`; new counters appended at the same point |
| One attempt per address, ever | ✔ | ✔ | Unchanged check; F-2 test proves the retry no longer releases it; `unconfirmed:` stays spent |
| Two instances over one directory agree | ✔ | ✔ | `_load()` under `flock` now also reloads `clients`/`stuck` (X: and `day`); pre-existing test passes; F-3 tests exercise a second instance |
| Nothing charged on refusal | ✔ | ✔ | All limit checks precede the charge; `status()["given"]` asserted after each refusal in the new tests |
| Pre-check refusals make no RPC call | ✔ | ✔ | Checks precede `get_code`; **valid test only in Y** (X-1) |
| Old-format state file (`{given, recent}`) loads | ✔ | ✔ | `test_old_format_state_file_loads` passes on both: records kept, hourly entry still counts, file upgraded on next write. Y's reuse of `recent` for the day means the daily count starts from ≤1 h of history after upgrade — harmless |
| Stale-cache / dry / in-flight / contract / checksum behaviour | ✔ | ✔ | All 12 pre-existing tests pass unchanged apart from the added `client` argument |

## Defects

### X (`tap-fix-x`)

**X-1 (Medium, test quality) — `test_per_client_cap_checked_before_any_rpc` passes for the wrong reason.**
`tests/test_tap.py`, the `monkeypatch.setattr(L.w3.provider, "make_request", ...)` spy. web3 7.16's
`RequestManager` calls `provider.request_func(w3, middleware)` which builds and *caches* a callable around
the original bound `make_request` (`providers/base.py:119-130`); replacing the instance attribute afterwards
is never observed. Evidence: `test_make_request_spy_sees_rpc` (same spy, a *served* give) records `[]` on
both worktrees. So `assert calls == []` holds unconditionally and the test proves nothing. Fix: spy on
`w3.eth.get_code` (Y's approach) or on `w3.manager.request_blocking`.

**X-2 (Low) — the F-2 `failed:` record and 502 do not carry the transaction hash.** `demo/tap.py::_send`:
`h` is only assigned when `send_raw_transaction` returns, so on the retry-error path the message is
`...not yet confirmed: {'code': -32603, 'message': 'already known'}`. The operator has no hash to `cast
receipt`. Evidence: `test_f2_record_names_the_hash` fails on X, passes on Y. Fix: sign first, take
`signed.hash` (Y's `_send`).

**X-3 (Low, cosmetic) — the stuck-record clear does a redundant `_save()`.** In X the clear sits *after* the
in-flight check, immediately before the charge's own `_save()`, so the extra write is always followed by
another. Harmless.

X shares XY-1 and XY-2 below.

### Y (`tap-fix-y`)

**Y-1 (Medium, test quality — flaky) — `test_unconfirmed_transaction_is_recorded_and_named` fails
intermittently.** `tests/test_tap.py`, the test sets `RECEIPT_TIMEOUT = 1` but leaves `RECEIPT_POLL = 2`.
web3's `wait_for_transaction_receipt` polls once, sleeps `poll_latency`, then checks the timeout, so when the
first poll of the *post-recovery* give (line "nonce cleared: serving again") lands before anvil has mined, the
healthy transaction is reported `Unconfirmed` after 2 s. Evidence: failed 2 of 3 isolated runs and 1 of 2 full
`tests` runs, traceback ending in `demo/tap.py:275: Unconfirmed`. Fix: add
`monkeypatch.setattr(tapmod, "RECEIPT_POLL", 0.2)` next to the `RECEIPT_TIMEOUT` patch (as X does);
verified 4/4 passes with that one line (`scratchpad/final2/test_y_fixed.py`).

**Y-2 (Trivial) — `give()` answers 400 for a non-string `client`.** That is a server programming error, not
visitor input; 500 would be the honest status. Unreachable from HTTP. Leave or change; not blocking.

Y shares XY-1 and XY-2 below.

### Both (identical code in `demo/serve_chain.py::tap_client`)

**XY-1 (Medium) — repeated `X-Forwarded-For` header lines: the first (client-supplied) line is used.**
`request.headers.get("x-forwarded-for")` returns the first line of a repeated header (Starlette
`Headers.__getitem__`). nginx merges repeated lines into one before appending, so behind nginx this is fine;
HAProxy `option forwardfor` (and any proxy that *adds* rather than appends) leaves the visitor's line first
and its own last, and the per-client cap is then bypassed with one extra header. Evidence:
`test_xff_multiple_header_lines` — 200 where 429 is expected, on both worktrees. Fix (two lines):
```python
lines = request.headers.getlist("x-forwarded-for")
if lines and lines[-1].strip():
    return lines[-1].split(",")[-1].strip()
```
Under nginx `getlist()` has one element, so behaviour there is unchanged. Add a test that sends two header
lines (the one in `scratchpad/final2/test_final2.py` can be lifted as is).

**XY-2 (Low, documentation) — the trust rule assumes exactly one appending proxy.** Neither docstring says
that `FOLIANT_TRUST_PROXY=1` (a) is fully forgeable if any request can reach the server without passing the
proxy, and (b) must not be set behind two appending hops (CDN → nginx), where the last entry is the CDN's IP
and every visitor shares one key — in that deployment configure nginx `real_ip_header`/`set_real_ip_from` for
the CDN and `proxy_set_header X-Forwarded-For $remote_addr`, or accept a per-CDN-egress cap. One paragraph in
`serve_chain.py`'s module docstring next to the existing note.

## Test quality

| Test (new) | X | Y | Note |
|---|---|---|---|
| per-client daily cap (refusal, no charge, other client served, restart, hash only) | ✔ | ✔ | Both assert the raw client never appears in the file and the sha256 key does. X additionally asserts `"busy" not in` the message (distinguishes from the hourly refusal). |
| per-client refusal makes no RPC call | ✘ vacuous (X-1) | ✔ (inside the cap test) | |
| global daily cap | ✔ | ✔ | X also checks the persisted `day` list length. |
| HTTP: peer vs trusted proxy, chained entries | ✔ | ✔ | Equivalent; X uses `PER_CLIENT_DAY=1`, Y the default 3. Neither covers repeated header lines (XY-1). |
| F-2: retry error with tx in pool → 502, spent, one mint; genuine refusal → released | ✔ synthetic error | ✔ genuine node error (double send) | Y's is the more faithful reproduction and also asserts the record names the hash. |
| F-3: unconfirmed record, stuck persisted, named 503 from this and a fresh instance, clears when mined, visitor stays spent | ✔ | ✔ but flaky (Y-1) | Y also asserts the 504 message names the hash and that the visitor's record is unchanged after the clear. Neither covers replaced/dropped; my two checks pass on both and could be added. |
| F-4 poll latency kwarg | ✔ | ✔ | |

Pre-existing tests: both branches change only the added `client` argument; the pre-existing
`test_failure_after_broadcast_spends_the_address` still raises a plain `TapError(504)` from a patched `_send`
and therefore exercises the *generic* `failed:` path, not `Unconfirmed` — fine, that is what its assertion
expects.

## Fidelity to the adjudicated fixes

Both follow F-1..F-4 as written: per-client counter in the same persisted state keyed by a hash, `X-Forwarded-For`
only under `FOLIANT_TRUST_PROXY`, a global daily cap, `give(address, client)`; F-2 decided by
`get_transaction_count(..., "pending") > tx["nonce"]` raising `TapError(502, "...accepted by the node but not yet
confirmed...")`; F-3 `unconfirmed:<hash>`, persisted `stuck {nonce, hash, since}`, named 503, docstring recipe,
no self-healing replacement (explicitly allowed to follow); F-4 `poll_latency=2`. Justified deviations: Y's
`Unconfirmed` carrying `nonce` and hash (both branches do this; it is the clean way to get the data to the
handler); Y signing before sending (better than the spec, see X-2); Y reusing `recent` for the day rather than a
new list (smaller state, no behavioural difference). Neither lowered the default hourly cap (optional per F-1).

## Merge plan

1. Merge `tap-fix-y` (3a1338d) onto d9c7d03. Do not cherry-pick code from X; the only thing X does better
   (patching `RECEIPT_POLL` in the F-3 test) is a one-line fix to Y.
2. Before merging, on `tap-fix-y`:
   - **Y-1**: in `tests/test_tap.py::test_unconfirmed_transaction_is_recorded_and_named`, add
     `monkeypatch.setattr(tapmod, "RECEIPT_POLL", 0.2)` after the `RECEIPT_TIMEOUT` patch.
   - **XY-1**: in `demo/serve_chain.py::tap_client`, use `request.headers.getlist("x-forwarded-for")[-1]`
     as shown above; add the two-header-line test.
   - **XY-2**: one docstring paragraph in `serve_chain.py` on the one-hop assumption and the no-proxy footgun.
3. Optional, same commit or later:
   - Add my replaced/dropped F-3 checks (`test_f3_replaced_stuck_tx_clears_record`,
     `test_f3_dropped_stuck_tx_clears_record_and_address_stays_spent`) and the old-format-file check to
     `tests/test_tap.py`; all pass on Y as is.
   - In `_send`, on the chain-check-positive branch, fall through to `wait_for_transaction_receipt(signed.hash)`
     instead of raising 502: Y already has the hash, and a lost response would then end as a normal `funded`
     rather than a spent address with tokens delivered and no AVAX. Deviates from the adjudicated F-2 text, so
     the maintainer's call.
   - Y-2 (400 → 500 for a non-string `client`), and a docstring line that `FOLIANT_TAP_PER_DAY` turns a
     rotating-IP drain into a day-long lock-out, so operators know which knob to turn.
4. `demo/TAP-AUDIT-FINAL.md` is committed on Y; `TAP-AUDIT-A.md`/`-B.md` remain untracked in the main tree —
   decide whether to add them alongside for the record (precedent: `TAP-REVIEW.md` is tracked).

Reproductions: `/tmp/claude-0/-home-claude/f52e2597-e0d8-5b12-943c-449c632a675a/scratchpad/final2/test_final2.py`
(run with `PYTHONPATH=<worktree> python3 -m pytest test_final2.py -q -s`; expected: X fails
`test_make_request_spy_sees_rpc`, `test_xff_multiple_header_lines`, `test_f2_record_names_the_hash`; Y fails the
first two) and `test_y_fixed.py` (Y's suite with the Y-1 line added).
