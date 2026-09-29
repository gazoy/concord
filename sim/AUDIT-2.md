# Independent audit 2 — 2026-09-29

Scope: commit 3dfe201 (with 0f77a97): `foliant/agent.py`, `foliant/ledger.py`, `foliant/accounts.py`, `tests/test_tree.py`, `sim/SPEC.md` v1.3, `sim/crosschain.py`, `sim/results.csv` (2,240 rows), `sim/RESULTS-auto.md`, `sim/RESULTS.md`. `sim/AUDIT-1.md` is byte-identical to my first report. I did not modify `foliant/` or `sim/`. New scripts and outputs are in `scratchpad/audit/a2/`:
- `mkmut.py` builds relay mutants of the **new** `crosschain.py` (late by 5 s, late by 30 s, relay writing into the leaf only), each with and without the §6 window invariant.
- `controls.py` runs those mutants.
- `adversarial2.py` and `fuzz2.py` rerun the first audit's sets against the new library.
- `qrun/` holds a mutant `--quick` sweep.

`pytest tests` passes (31 tests).

## Verdict table

| Item | Verdict | Evidence |
|---|---|---|
| D1 signer charged by refused spends | **VERIFIED FIXED** | `agent.py:38,47-50` snapshots the signer window and restores it on any exception from `ledger.apply`. With the unmodified harness, the new library now gives exactly the numbers my first audit got with the signer fix applied by hand (d=60 seed 0: 674, seed 1: 687, d=10 seed 1: 115). Signer refusals in those runs are 0. The simulation counts signer and ledger refusals separately (`crosschain.py:107-108,159-165`); the classification is correct because the signer window is back to its pre-attempt state when it is read. |
| D2 depth 2 identical to depth 1 | **VERIFIED FIXED** | `crosschain.py:73-81`: workers and sub-workers (cap c_w/2) both spend. In `results.csv`, 0 of 1,120 depth-1/depth-2 pairs are identical. |
| D3 H2 insensitive | **VERIFIED FIXED** | `crosschain.py:109,170,187-188,225`. The per-event bound is evaluated in every run: `tight_violations` = 0 in all 2,240 rows, and 1,861 rows have non-zero overshoot, so the check actually ran on those. Positive controls are in item 2 below. |
| D4 `bound_caps` vacuous | **PARTLY** | `bound_caps = min(C, max side caps)` and `slack_caps` are added (`:206-208,226`). At depth 2, `side_caps` uses `1.5·c_w·n`, which is wrong: a sub-worker's spends are recorded in its worker's window, so a worker's subtree is capped at c_w. See N1. |
| D5 H2 counted at d=0 | **VERIFIED FIXED** | Row flag at `:225`. Summary subsets at `:274-275`: H1 over d=0 (320), H2 over d>0 (1,920), H3 and H4 over all runs (2,240), H5 over depth 2 with d>0 (960). These match SPEC 1.3 §4 and RESULTS-auto. |
| D6 1,801 spend seconds | **VERIFIED FIXED** | `:147` `step < P.duration` |
| D7 invariant 3 / abort semantics | **PARTLY** | Fixed: the lineage check now runs at spend time (`:152-156,166-167`); a failure is caught per run (`:89-93`); rows are written as runs finish (`:266-272`). Not fixed: `write_report` crashes on any invariant-error row (N2); the supply and window invariants are still bare `assert` (`:193,200`), so `python -O` disables them (N5). The lineage check re-implements the ledger's own formula and only checks one direction (N6). |
| D8 stale docstring | **VERIFIED FIXED** | `:1` cites v1.3 |
| D9 child-id wording | **VERIFIED FIXED** | SPEC §2.2 |
| D10 H4 adds no independent evidence | **PARTLY** | RESULTS.md:87-89 records that H4 was restated. It does not say that H4 (1.1) adds nothing beyond the §6 window invariant, and it does not report `overshoot_after_last` as a measured drain time. H4 still appears in the results table as a plain pass. |
| D11 escalation bypass / `within()` | **VERIFIED FIXED** (escalation); **ACCEPTED BY DESIGN** (`within()`) | `ledger.py:136` passes `escalated and a is acct`. My first-audit reproduction (a child spending 500 under a root with `per_tx_max=10`) now raises. Regression test at `tests/test_tree.py:134`. `within()` still ignores `window_secs` and `escalation`; the reasons are documented at `accounts.py:96-99` and hold, because ancestors still bind the subtree. |
| D12 windows recorded before later failures | **VERIFIED FIXED in code; NO REGRESSION TEST** | `ledger.py:177-179,181-185,187-194,215-222`. Checked at runtime: an `InsufficientFunds` transfer leaves the child's, the root's and the signer's windows at 0. However, the test labelled "insufficient funds path" (`tests/test_tree.py:159`) actually raises `PolicyViolation: amount 1000 exceeds per_tx_max 100` from the signer and never reaches the ledger (N3). RESULTS.md:103-104 says both library defects are fixed "with regression tests"; that is not true for this one. |
| D13 reports not produced by code | **PARTLY** | `write_report` (`:285-315`) produces `RESULTS-auto.md` and `overshoot.png`, but crashes whenever a row has an invariant error (N2). |
| D14 / threat 1 split-brain | **VERIFIED FIXED (documented)** | SPEC §8 `:178-185`; RESULTS.md:75-78. It remains a protocol gap, correctly marked as not established. |
| Threat 2 clock skew | **VERIFIED FIXED (documented)** | SPEC §8 `:186-189`; RESULTS.md:74 |
| Positive controls on the new checks | **CONFORMS** | See item 2 |
| Adversarial and fuzz on the new library | **CONFORMS**; magnitudes up | See item 3 |
| RESULTS.md | **CONCERN** | Seven overstatements or misattributions, plus one forward reference; see item 4 |
| New defects | **DEFECT** | N1–N8 |

## 1. Checks requested on the sweep

- **Depth 2 differs from depth 1:** yes, 0 of 1,120 pairs are identical. Depth-2 overshoot is larger because each worker's subtree has two concurrent spenders, which doubles its attempt rate:

  | d | max overshoot, depth 1 | max overshoot, depth 2 |
  |---|---|---|
  | 1 | 31 | 34 |
  | 5 | 79 | 157 |
  | 10 | 143 | 259 |
  | 30 | 371 | 734 |
  | 60 | 783 | 1,000 |
  | 300 | 1,000 | 1,000 |

- **Per-event bound (`tight_violations`) is evaluated:** yes, in every second with overshoot > 0 (`:187-188`), against the unseen remote amount captured at the latest applied spend (`:170`). The definition matches the proof in AUDIT-1 §4 and SPEC 1.3 §3. It is 0 in all 2,240 rows.
- **Subsets:** as SPEC 1.3 states (see D5 in the verdict table). Every row passes: 320/320, 1,920/1,920, 2,240/2,240, 2,240/2,240, 960/960. There are 0 invariant errors.

## 2. Positive controls against the new checks

These are mutants of the new `crosschain.py`: 5 seeds each, c_w=1000, (2,2), p=0.5, 1,200 s.

**Mutants with the §6 window invariant disabled.** These test H2 (a) and (b) on their own:

| Mutant | depth 1, d = 1 / 10 / 60: whole-run (a) | depth 1: per-event (b) | depth 2, d = 1 / 10 / 60: (a) | depth 2: (b) |
|---|---|---|---|---|
| relay 5 s late | 5/5, 1/5, 1/5 | **5/5, 5/5, 5/5** | 5/5, 3/5, 0/5 | 5/5, 5/5, 3/5 |
| relay 30 s late | 5/5, 5/5, 5/5 | 5/5, 5/5, 5/5 | 5/5, 5/5, 0/5 | 5/5, 5/5, 3/5 |
| relay writes into leaf only | 5/5, 5/5, 5/5 | 5/5, 5/5, 5/5 | 5/5, 5/5, 0/5 | 5/5, 5/5, 5/5 |

**With the window invariant enabled (the shipped configuration),** every mutant run ends in an invariant error and fails H2: 90 of 90 runs.

So the per-event bound now catches the 5 s case, 28 of 30 without the invariant, where the whole-run bound caught 15 of 30. The misses are at depth 2 with d=60, where the tree is saturated at C: a late relay produces no overshoot the bound can see. That is a limit of any output-based check (N7), and the window invariant remains the primary detector.

## 3. Adversarial and fuzz sets against the new library

All 32 adversarial runs and all 400 fuzz runs passed H1, H2, the tight bound, H3 and H4, with 0 ground-truth mismatches and a maximum ratio of 1.000. Signer refusals fell to 0 in every scenario where the leaf's own cap never binds.

Overshoot rose wherever the signer defect had throttled traffic:

| Scenario | Before | After |
|---|---|---|
| Many workers (10,10), d=1 | 31 | 72 |
| m=C, d=300 | 796 | 976 |
| Depth 2 concurrent, random, d=1 | 13 | 34 (not like for like: the new build gives sub-workers c_w/2) |
| Depth 2 concurrent, random, d=30 | 623 | 667 (same caveat) |
| B-first ordering, d=30 | 346 | 370 |

Saturating scenarios were unchanged at 1,000.

Like-for-like depth-1 comparison of the sweep (old results against the new `results.csv`):

| d | 1 | 5 | 10 | 30 | 60 | 300 |
|---|---|---|---|---|---|---|
| old max overshoot | 17 | 73 | 138 | 369 | 686 | 1,000 |
| new max overshoot | 31 | 79 | 143 | 371 | 783 | 1,000 |

The effect of the signer fix alone is therefore +14% at d=60 and +82% at d=1. That is more than the "up to about 12%" RESULTS.md takes from my first report, which was based on only a few runs.

## 4. RESULTS.md review

Supported by `results.csv` or my own findings: the assumptions list (:10-16); the pass table (:37-43); the worst-case table (:45-55, which matches RESULTS-auto); saturation at C and a 2C total (:57-59); "every invariant in SPEC §6 held" (:29, 0 invariant errors); the list of what is not established (:73-79); and the history of H4 and d=0 (:87-92).

Overstated or wrong:
1. **:31-33** "mutated relays (late, or writing into the wrong account) are caught by the checks (AUDIT-1 §4, positive controls)". The cited source says the opposite for a 5 s late relay: H2 missed it in 8 of 15 runs. The statement is true **now** (item 2), so it should cite this audit. The mutant tested was "writes into the leaf only (ancestors missing)", not "wrong account".
2. **:29-31** "adversarial traffic reaches the bound exactly (ratio 1.00 at d ≥ 60 …)". In `results.csv`, the ratio of 1.00 at d=60 comes only from depth-2 runs (depth 1 reaches 0.96). At d ≥ 60 it is a ceiling effect: overshoot and `bound_rate` both equal C. Non-saturated tightness is shown only in my harness (ratio 1.00 at d=1 under blast).
3. **:67-70** "exactly the classic eventually-consistent bound, overshoot ≤ remote rate × delay, with no compounding through delegation depth and no dependence on spend pattern beyond rate".
   - The bound is the remote amount applied in a d-length interval (a burst measure), not average rate × delay. Achieved overshoot depends strongly on pattern: the median is a fraction of the maximum.
   - Depth was tested only to 2. Beyond that, "no compounding" rests on the proof, and the text should say so.
   - "The whitepaper may state that as a specified property" needs the assumption list attached, including the unenforced one-home-ledger rule.
4. **:93-99** The signer defect understated overshoot "by up to about 12%": the like-for-like figures are +14% (d=60) and +82% (d=1). "686 → 1,000 at d = 60" compares old depth-1 runs with new depth-2 runs, whose traffic model changed. Like for like, it is 686 → 783. The sentence attributes to the correction an increase that is partly a model change.
5. **:96-97** "the H3 bound could not bind in most cells. All four were fixed". H3 is reached only at c_w=300, (2,2), depth 1 (overshoot 200 = slack 200). At depth 2 its bounds are inflated (N1). In c_w=1000 cells neither bound can bind below C.
6. **:103-104** "Both are fixed with regression tests". D12 has no working regression test (N3).
7. **:5-6** refers to `AUDIT-2.md` as "reproduced unedited" before it exists. This is acceptable only if this report is appended verbatim.
8. **:39-43** H4 is listed as a pass with no note that it is implied by the §6 window invariant (D10).

## 5. Remaining and new defects

1. **N1, should-fix.** The depth-2 cap bounds are inflated. `side_caps = c_w·n·1.5` (`sim/crosschain.py:206`), but a sub-worker's spends are recorded in its worker's window, so each worker subtree is capped at c_w. In the c_w=300, (2,2), depth-2 cell the code uses `bound_caps` 900 (should be 600) and `slack_caps` 800 (should be 200). Observed max overshoot there is 200, so a violation between 200 and 800 would pass H3. The code also contradicts SPEC 1.3 §3 ("sum of leaf `per_window_max`"). Fix: `side_caps = c_w·n` at both depths. Tighten slack to `Σ_side min(C, side_caps) − C`: this equals 300 at c_w=300, (1,4), where overshoot reaches exactly 300, so H3 would then bind in two combinations instead of one.
2. **N2, should-fix.** `write_report` raises `KeyError: 'overshoot_max'` whenever any row has an invariant error, because such rows carry only parameters and flags (`sim/crosschain.py:89-93` vs `:292,310-313`). Reproduced with the 30 s late mutant `--quick`: the CSV is written, the report and figure are not. Fix: exclude invariant-error rows from the figure and tables, list them explicitly, and give them the metric fields as None.
3. **N3, should-fix.** There is no regression test for D12, and the existing test is mislabelled. `tests/test_tree.py:159` exceeds the signer's `per_tx_max`, not the funds. Fix: use a policy with `per_tx_max` ≥ the amount and a fund below it. Assert `InsufficientFunds`, and assert that the leaf's and the root's ledger windows and the signer window are all unchanged. Add the same for `open_channel` on an existing channel id and for `join_pool` by an active member.
4. **N4, blocking for the library (pre-existing, not introduced by the fixes; not affecting the cross-ledger claim).** Negative transfers inside the tree create money. `_authorise` returns early for payees inside the tree (`ledger.py:128`) with no amount check. `_require_funds` passes for a negative amount (`:177-179`), and `_move` (`:52-57`) accepts it. A child envelope signed directly with its key (bypassing the enclave, which the ledger must not rely on) with `amount=-5000` to the root left the child with 5,510 and the root with −4,510. It moves value up and down without `recall` authority and drives balances negative. Spends out of the tree are still capped, so the budget claim stands, but the whitepaper's ledger-integrity claims do not. The same applies to `_op_recall` with a negative `amount`. Fix: reject `amount ≤ 0` in `_move` (or in every op) and in `recall`, and add a test.
5. **N5, note.** The supply and window invariants are still bare `assert` (`sim/crosschain.py:193,200`), so `python -O` silently disables them. Fix: `if …: raise InvariantError(…)`.
6. **N6, note.** The spend-time lineage invariant (`:155-156`) repeats the ledger's own formula and checks only one direction (accepted ⇒ room). It would not catch a ledger that wrongly refuses, and it shares any mistake in the formula. Fix: also assert "refused by the ledger ⇒ some account lacked room or `per_tx_max` was exceeded", and compute room from `applied` (ground truth) rather than the ledger's window objects.
7. **N7, note.** A relay fault cannot be seen in the output while the tree is saturated at C (item 2: 2 of 5 misses at depth 2, d=60). RESULTS should say that the window invariant is the primary detector and H2 (b) the secondary one.
8. **N8, note.** `Agent.submit` rolls back on any exception (`agent.py:47-50`). That is correct only while every ledger op validates before it records. A future op that records and then raises would make the signer under-count, the reverse divergence. Fix: keep the validate-before-record rule as a documented ledger invariant, and add a test that asserts signer window = leaf ledger window after a random mix of accepted and refused ops.
9. **N9, should-fix.** The RESULTS.md wording issues listed in item 4 (points 1–6 and 8).

## Conclusion

The fixes are real. D1, D2, D3, D5, D6, D8, D9 and the escalation half of D11 are verified. D12 is fixed in code but lacks the regression test RESULTS.md claims. D4, D7, D10 and D13 are only partly fixed. The new sweep passes every hypothesis over the subsets SPEC 1.3 specifies. The per-event bound is actually evaluated, and it now catches a relay 5 s late (28 of 30 without the window invariant; 90 of 90 mutant runs are caught with it). My adversarial and 400-run fuzz sets still find no violation against the new library.

The overshoot magnitudes rose once the signer throttling was removed: like for like at depth 1, d=60 went from 686 to 783, and some adversarial cases more than doubled. The claim stated in RESULTS.md:18-24 is supported under its listed assumptions. It is a theorem, confirmed by a simulation whose checks are shown to be able to fail.

Before RESULTS.md is final, it should:
- drop "exactly the classic … no dependence on spend pattern beyond rate";
- correct the attribution of 686 → 1,000 and the "up to about 12%" figure;
- cite this audit, not AUDIT-1, for the positive controls;
- stop claiming a regression test for D12.

N1 (inflated depth-2 cap bounds) and N2 (report crash) should be fixed in the code. Separately, the library has a pre-existing money-creation bug (N4: negative transfers inside the tree) that must be fixed before any integrity claim in the whitepaper, though it does not affect the budget bound studied here.
