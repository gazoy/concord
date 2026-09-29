# Cross-chain budget tree: results

29 September 2026. Specification: `SPEC.md` v1.3. Code: `crosschain.py`. Data:
`results.csv` (2,240 runs), `RESULTS-auto.md`, `overshoot.png`. Independent
audits: `AUDIT-1.md` (on spec 1.2 and the first sweep), `AUDIT-2.md` (on the
fixes and this sweep), both reproduced unedited.

## The claim, as supported

Under these assumptions:

- two ledgers running the unmodified reference tree code;
- an honest, lossless, in-order relay that delivers every spend to the other
  ledger exactly `d` seconds after it was applied;
- one shared clock;
- each branch of the crew spending on one ledger only, through `transfer`;

the crew's committed value never exceeds the root's cap `C` by more than the
amount the other ledger applied in the `d` seconds before the last local
spend. That amount is at most the largest the remote side applied in any
interval of length `d` (`bound_rate`), at most `min(C, the remote side's own
leaf caps)`, and at most the sum of all leaf caps minus `C`. Once a spend is
older than `d` both ledgers have it, so refusal recovers within `d`; the
sliding window then drains over `W`.

The first audit showed this is a theorem given the model (AUDIT-1 §4), so the
simulation's job is to show that the implementation matches the model and that
the checks can fail, not to estimate a probability. Both are shown. Every
invariant in SPEC §6 held every second of every run. The bound is reached: at
d ≥ 60 the ratio is 1.00 in the sweep, though that is partly a ceiling effect
(both overshoot and the bound equal C); tightness away from saturation is
shown in the audit's harness, where saturating traffic reaches ratio 1.00 at
d = 1 (AUDIT-1 §4). Mutated relays (5 s late, 30 s late, writing only into
the leaf) are caught: the per-event H2 check catches the 5 s case in 28 of 30
runs on its own, and the window invariant catches all 90 mutant runs
(AUDIT-2 §2). The window invariant is the primary detector; H2 (b) is the
secondary one, and neither can see a relay fault while the tree is saturated
at C.

## Results

| Hypothesis | Runs | Pass |
| --- | --- | --- |
| H1 zero delay is safe | 320 (d = 0) | 320 |
| H2 rate × delay bound, whole-run and per-event | 1,920 (d > 0) | 1,920 |
| H3 cap bounds | 2,240 | 2,240 |
| H4 refusal lag ≤ d (implied by the §6 window invariant; kept as a stated property, not independent evidence) | 2,240 | 2,240 |
| H5 H2 at depth 2 with concurrent workers and sub-workers | 960 | 960 |

Worst overshoot by delay (units; C = 1000, W = 600 s):

| d (s) | max overshoot | max overshoot / bound |
| --- | --- | --- |
| 0 | 0 | — |
| 1 | 34 | 0.79 |
| 5 | 157 | 0.90 |
| 10 | 259 | 0.87 |
| 30 | 734 | 0.95 |
| 60 | 1,000 | 1.00 |
| 300 | 1,000 | 1.00 |

The overshoot saturates at C: each ledger caps its own local spends at C, so
with a long enough delay the remote side can commit a full cap unseen and the
tree can reach 2C in total. That is the honest worst case for a two-ledger
crew with no reservation mechanism, and it is why a real design would either
split the cap between ledgers or run the tree on one ledger.

![overshoot](overshoot.png)

## What this does and does not establish

Established: overshoot is bounded by the amount the remote side applied in
the d seconds before the last local spend, a burst measure, not an average
rate × delay; achieved overshoot depends strongly on the spend pattern, with
the median a fraction of the maximum. No compounding through delegation depth
was observed to depth 2, and the proof in AUDIT-1 §4 does not depend on depth.
The whitepaper may state the bound as a specified property with `d` as the
parameter, with the assumption list above attached, including the unenforced
rule that each branch has one home ledger.

Not established: anything about a relay that lies, drops or reorders; clock
skew (a window-membership error, not a delay; SPEC §8); the behaviour of
channel deposits, pools or streams across ledgers; or the "own local caps"
clause beyond one ledger per branch, which the audit falsified for a branch
funded on both ledgers (300 + 300 against a cap of 300). A protocol spanning
ledgers must bind each account to one home ledger. None of this was in scope,
and the whitepaper's open-problems paragraph should say so.

## What changed during the study, and why

Recorded in full in the SPEC change log; summarised here because a reader of
the results should know the hypotheses were not all stated correctly at the
start.

- H4 as first written required committed value to fall within `d` of the last
  spend; that confused relay catch-up with window drain and failed on the
  first smoke run. It was restated as refusal lag ≤ d.
- "d = 0" first allowed same-second concurrency on the two ledgers, giving
  overshoot at zero delay; it was made synchronous. Sub-second concurrency is
  represented by d = 1, which is the honest lower bound for a live system.
- The first audit found that the enclave signer charged its window before the
  ledger decided, throttling traffic after saturation and understating
  overshoot; that depth-2 runs were identical to depth-1 runs; that the H2
  check was insensitive to a slightly late relay; and that the H3 bound could
  not bind in most cells. All four were fixed and the sweep rerun. Like for
  like at depth 1, the signer fix alone raised the worst overshoot from 686 to
  783 at d = 60 (+14%) and from 17 to 31 at d = 1 (+82%); the 1,000 at d = 60
  in the table comes from the new depth-2 runs, whose traffic model also
  changed (two concurrent spenders per worker subtree). The second audit
  found the depth-2 cap bounds still inflated; the H3 columns in results.csv
  were recomputed from the same runs with the corrected formula (a derived
  column; no run was changed), after which H3 binds in 248 runs across six
  cells instead of one.
- The audits also found three defects in the library that the simulation
  did not exercise but the whitepaper's claims depend on: a child's
  escalation co-signer lifted every ancestor's per-transaction cap; windows
  were recorded before later validation could fail; and a negative transfer
  to an address inside the tree was accepted, creating money (pre-existing,
  found in the second audit, unrelated to the budget bound). All three are
  fixed with regression tests (`tests/test_tree.py`), and a property test now
  checks that the enclave's window equals the ledger's after any mix of
  accepted and refused operations.

## Reproduction

    python sim/crosschain.py            # full sweep, about an hour
    python sim/crosschain.py --quick    # 26 runs, a few seconds
    python -m pytest tests/test_tree.py

Runs are deterministic in (parameters, seed); `results.csv` carries both.


---

# Appendix A: independent audit 1 (unedited)

# Independent audit — 2026-09-29

Scope: `sim/SPEC.md` v1.2 (including change log), `sim/crosschain.py` (commit ebc1918), `foliant/ledger.py`, `foliant/accounts.py`, `foliant/agent.py`. Data: `sim/results.csv` (full sweep, 2,240 runs, finished during the audit using the 1.2 code; the process started at 08:55:42, one second after the 1.2 commit), plus my own runs. Audit scripts and outputs are in `scratchpad/audit/` (`harness.py`, `adversarial.py`, `fuzz.py`, `mutation.py`, `fresh.py`, and their `.out`/`.csv` files). I did not modify `foliant/` or `sim/`.

`harness.py` is a copy of `run()` with hooks added (a pluggable spend pattern, refusal source tagging, a tight per-spend bound, ground truth rebuilt from ledger logs, an optional fix for the signer, and relay mutants). On 10 parameter/seed combinations it gives exactly the same results as `sim.crosschain.run`.

## Verdicts

| # | Item | Verdict |
|---|---|---|
| 1 | Spec-to-code conformance | **DEFECT**: §2.4 is violated, because a refused attempt does have an effect. §6 invariant 3 is only partly implemented. There are several small divergences. |
| 2 | Ground truth and metrics | **CONFORMS**, with concerns. The ground truth is correct and independent of the ledgers. `bound_rate` is valid but not very sensitive. `bound_caps` is valid but cannot bind in three of the four (c_w, n) combinations. |
| 3 | Tree code | **CONFORMS** for the path the simulation exercises. **CONCERN** for the library outside that path (escalation bypass, `within()` gaps, non-atomic recording, split-brain across ledgers). |
| 4 | Try to break H2 | **CONFORMS**. No violation in 32 adversarial runs or 400 fuzz runs. The bound is reached exactly (ratio 1.00), so the comparison means something. |
| 5 | Fresh seeds 100–109 | **CONFORMS**. All 112 cells × 10 seeds pass H1–H5. |
| 6 | Statistical adequacy | **CONCERN**. H2 is a deterministic theorem, so seed counts are the wrong kind of evidence. Half the sweep is exact duplicates. There are no positive controls. |
| 7 | Threats to validity | **CONCERN**. §8 misses several material threats, including one that falsifies the "own local caps" half of the claim once a branch can spend on both ledgers. |

---

## 1. Spec-to-code conformance

| Spec element | Code | Verdict |
|---|---|---|
| §2.1 two unmodified `Ledger`s, one shared clock | `build_side` `crosschain.py:62`; clock set for both at `:118-119` | Conforms |
| §2.2 root `per_window_max=C`, `window=W`, `per_tx_max≥C` | `:63` (`per_tx_max=C`) | Conforms |
| §2.2 workers with `per_window_max=c_w`, mirrored ids, funded only on home ledger | `:67-72`, `:77-78` | Conforms. Minor spec wording issue: child ids are `hash(parent_id, signer, salt)` (`accounts.py:165`), not "owner key, signer key and salt". They are still deterministic, and the relay delivers into the right accounts, as shown by the §6 invariant and by my checks. |
| §2.2 optional sub-worker level | `:74-76` | Conforms structurally. However, at depth 2 **only the sub-worker spends** (`:73-78`: `leaf` is appended and the worker is not). Because sub-worker and worker have the same cap `c_w`, a depth-2 run is **bit-for-bit identical** to the depth-1 run with the same seed: 1,120 of 1,120 pairs in `results.csv` match on `overshoot_max`, `refused`, `applied` and `bound_rate`. See defect D2. |
| §2.2 spends leave the tree and go through `_authorise` | `OUTSIDE` `:21`; `ag.transfer` `:139` → `ledger.py:175` | Conforms |
| §2.3 outbox of (chain, t, amount); delivery exactly d s later, in order, into the same accounts; nothing else exchanged | `Spend` `:40-45`; `deliver()` `:122-128`, called at `:129` (start of second, before spends), `:148` (after each spend) and `:151` | Conforms. For d≥1, entries with `t+d ≤ now` are delivered before that second's spends, which matches the stated consequence "A-local ≤ t plus B-local ≤ t−d". For d=0 delivery is per spend, as stated in change log 1.2. Nothing is re-delivered (`popleft`), nothing echoes back (relayed entries never enter an outbox), and the destination is `other[s.name]`. |
| §2.4 Bernoulli(p) per worker per second, amount U[1,m], one seeded RNG | `:136-137` | Conforms. Note that A's spenders always act before B's within a second (`:134`, dict order). This is deterministic priority, and the spec does not mention it. |
| §2.4 "an attempt the **local ledger** refuses … **has no effect**" | `:138-142` | **DEFECT (D1).** `Agent.transfer` → `Agent.submit` calls `AgentSigner.sign_payment` (`agent.py:39`), which **records the amount in the enclave-side SpendWindow** (`accounts.py:204`) *before* `ledger.apply` (`agent.py:44`). When the ledger then refuses because the root is at its cap, the leaf's signer window has already been charged. Later attempts are refused by the signer, not by the ledger, even when the ledger would accept them. Measured: in d=60, c_w=1000, (2,2), p=0.5, seed 0, 752 of 1,001 refusals came from the signer, and the signer window exceeded the leaf's ledger window by 708 at the end of the run. |
| §2.4 duration | loop `:116`, spend guard `:133` `step <= P.duration` | **Divergence (D6)**: spends happen on 1,801 seconds, not 1,800. |
| §2.5 ground truth (t−W, t], outside the ledgers | `true_committed` `:105-106` | Conforms. The interval matches `SpendWindow.spent`, which keeps `t > now−W` (`accounts.py:141-142`). I rebuilt it independently from each ledger's transfer log: it matched in every harness run (`gt=0`, `logov == overshoot`). |
| §3 `overshoot_max`, `overshoot_secs` | `:153-157` | Conforms. Measured once per second after all spends and the final delivery; within a second, committed value only rises, so this is the maximum for the second. |
| §3 `refused` | `:140-141` | **Divergence (part of D1)**: counts signer refusals and ledger refusals together. The spec defines it as refusals by the ledger. |
| §3 `bound_rate` | `:111-114`, `:160-161` | Conforms literally: the maximum over every t in [T0, T0+duration+d] and over both sides of the remote amount in (t−d, t]. Two notes. (a) It is ground truth; "as seen from A" in the spec is loose wording. (b) At d=0 it is 0 by construction. |
| §3 `bound_caps` | `:187` | Conforms to the 1.1 wording. It is structurally loose (D4). |
| §3 `h4_violations` | `:131`, `:149-150` | Conforms. `seen_by_all` is taken after the start-of-second delivery and before spends. |
| §4 H1 | `:199` | Conforms |
| §4 H2 "for d > 0" | `:200` | **Minor divergence (D5)**: the flag is also computed for d=0 rows, where `bound_rate=0` makes it the same as H1. The printed "H2 PASS 2240/2240" therefore includes 320 d=0 rows. |
| §4 H3 | `:201` | Conforms, but cannot fail wherever max side caps ≥ C, which is every combination except c_w=300, (2,2) (D4) |
| §4 H4 (1.1) | `:202` | Conforms. It adds no evidence beyond the §6 window invariant (D10). |
| §4 H5 | `:244` (only printed, no CSV column) | Conforms literally ("H2 holds with depth 2"). It is vacuous because depth 2 duplicates depth 1 (D2). |
| §5 sweep | `:206-223` | Conforms: 7×2×2×2×2 = 112 cells, seeds 0–19, W=600, C=1000, m=20. |
| §6 supply conserved | `:163-164` | Conforms. It is trivial, since transfers conserve supply by construction. |
| §6 window = known subtree spends, every second, every account | `:166-171` | Conforms. This is the strongest check in the code: it would catch relay double counting, wrong accounts or wrong timing. |
| §6 "no applied spend exceeded **its account's** own `per_window_max` on its local ledger at the time of the spend", checked every second | `:173-183` | **DEFECT (D7)**: checks only **leaves**, after the fact, rebuilt from the harness's own `applied` list rather than from the ledger at spend time. The intermediate worker (depth 2) and the root, against their *local view*, are never checked. |
| §6 "failure aborts the run and is reported" | bare `assert` | **Divergence (D7)**: a failure raises out of `sweep()`, kills the whole sweep and loses every row, because the CSV is written only at the end (`:234-238`). Running with `python -O` disables all the checks. |
| §7 outputs | `:229-245` | `RESULTS.md` and `overshoot.png` are not produced by the code (D13). |
| Module docstring `:1` "Implements sim/SPEC.md v1.0 exactly" | | Stale; the spec is v1.2 (D8). |

## 2. Ground truth and metrics

- **`true_committed`** (`:105-106`): correct. The half-open interval (t−W, t] matches the ledger's pruning. It is independent of the ledgers because it is built from `applied`, which records only after `transfer` returns. I cross-checked it against the ledgers' own transfer logs (`harness.py`, `gt_mismatch`): 0 mismatches in every audit run, including fuzz with W ∈ {10…100} and d ∈ {0, 1, W−1, W, W+1, 2W}.
- **`seen_by_all`** (`:108-109`): (t−W, t−d] is correct for the delivery timing in `:125`. For d ≥ W the interval is empty. That is correct: relayed entries arrive already expired and are pruned on the next `spent()` call.
- **`remote_in_delay_window`** (`:111-114`): correct. It matches what is actually undelivered at each moment. My harness asserts, at every applied spend, that the undelivered outbox sum equals the (s−d, s] sum (`harness.py:108`), and it never fired for d>0.
- **Relay accounts (`chain`)**: `chain_ids` (`:82-83`) is the local `lineage`, leaf first through to the root, and ids are identical across the two ledgers. The §6 window invariant (`:166-171`), checked every second on every account on both ledgers, passed in all 2,240 + 1,120 runs. So there is no double counting, no wrong-account delivery and no re-delivery.
- **Is H2 meaningful?** Yes. The bound is often reached, so it is not trivially large:

  | d | max overshoot (results.csv) | max overshoot / bound_rate | runs with ratio ≥ 0.95 |
  |---|---|---|---|
  | 1 | 17 | 0.53 | 0 |
  | 5 | 73 | 0.72 | 0 |
  | 10 | 138 | 0.83 | 0 |
  | 30 | 369 | 0.88 | 0 |
  | 60 | 686 | 0.95 | 0 |
  | 300 | 1000 | 1.00 | 80 |

  Under adversarial load (item 4) the ratio is exactly 1.00 at d = 1, 60, 300, 599 and 600. Two caveats remain:
  - (a) At d=300 the ratio of 1.0 is partly a ceiling effect. Both overshoot and `bound_rate` hit C, because each ledger caps its own local spends at C.
  - (b) `bound_rate` is a maximum over the whole run, not the unseen amount *at the time of the overshoot*. That weakens it as a detector (D3). In a positive-control run where the relay was secretly 5 s later than d, H2 flagged it in only 1 of 5 runs at d=10 and d=60. With 30 s extra lag it flagged 5 of 5, and a relay that writes only into the leaf was flagged 5 of 5. In the real code the §6 window invariant would catch a late relay, so this weakness matters only when H2 is taken as the evidence.
- **Magnitudes are understated (D1).** With the signer rollback fixed (`fix_signer=True`) and everything else unchanged, 1,800 s runs at c_w=1000, (2,2), p=0.5 gave: d=60 seed 0 overshoot 599→674, `bound_rate` 649→732; d=60 seed 1 619→687; d=10 seed 1 101→115; `overshoot_secs` up in every case. The pass/fail results do not change (the bound holds either way), but any overshoot magnitude reported in RESULTS.md from the current code is too low.

## 3. Tree code

- **`_authorise`** (`ledger.py:124-135`): a two-pass design (check every ancestor, then record in every ancestor). The ledger is single-threaded, so a partial record from a `PolicyViolation` is impossible. `SpendWindow.spent` filters by timestamp, so relayed entries arriving out of timestamp order are handled correctly. **In the simulation's path (plain `transfer` to an address outside the tree) I found no way for a ledger to apply a spend its local view of the tree should refuse.** The `h4` counter, the window invariant and a check against ledger logs all agree.
- **`_tree_addresses`** (`:119-122`): returns the root plus all descendants. `OUTSIDE` is a plain key address, so it cannot collide with `account:…`. Moves inside the tree are exempt by design. That means a worker can move funds to a sibling and spend them against the sibling's cap. This evades per-worker caps but not the root cap. It is not exercised by the simulation.
- **`deliver()`** (`crosschain.py:122-128`): sends only `s.outbox` to `other[s.name]`. Outbox entries are only local spends (`:145`). Relayed entries go straight into `SpendWindow.record` and never enter the receiving side's outbox, so nothing is delivered back to the ledger it came from, and `popleft` prevents re-delivery. Conforms.
- **Library defects found along the way** (outside the simulated path, but relevant to the whitepaper's §6.3a claim):
  - **Escalation bypasses every ancestor's `per_tx_max`.** `apply` checks the escalation signature against the *leaf's* `policy.escalation` (`ledger.py:158-163`). `_authorise` then passes `escalated=True` to **every ancestor's** `check` (`:133`), and `Policy.within` (`accounts.py:89-105`) does not constrain the child's `escalation`. A parent can therefore give a child the ability to exceed the root's per-transaction cap. Reproduced: a child spent 500 under a root with `per_tx_max=10`. Per-window caps are not affected.
  - **`within()` ignores `window_secs`.** A child with `window_secs=1` under a parent with 600 was accepted, so the child's own cap means nothing. Ancestors still bind.
  - **Windows are recorded before later checks.** `_op_transfer` records (`:175`) before `_move` (`:176`), which can raise `InsufficientFunds`. `_op_open_channel` records (`:182`) before the "channel exists" check (`:185`). A failed transaction leaves phantom window entries. This errs toward over-refusal, never toward over-spending.
  - **D1 is a library defect** (`agent.py:39-44`, `accounts.py:200-205`). It errs toward over-refusal, but it makes the enclave's window disagree with the ledger's, which contradicts the module docstring ("a signer that refuses to sign and a ledger that refuses to apply agree exactly").

## 4. Trying to break H2

H2 follows from the model. Let s be the last applied spend event at or before t, on side X, and Y the other side. Every X spend inside the window at t is at or before s, and every Y spend inside the window at t is at or before s. X's view at s included all of Y's spends up to s−d and was ≤ C after the spend. So `committed(t) − C ≤ Y(s−d, s]`, which is ≤ `bound_rate`, and also ≤ min(C, Y's caps). So a counterexample would indicate an implementation bug, not a false hypothesis. I tried to break it anyway, and also checked this tighter per-spend bound ("tight"). Results are in `adversarial.out`, `fuzz.out` and `mutation.out`.

| Attempt | Parameters | overshoot / bound_rate / tight | H2 |
|---|---|---|---|
| Both sides send max-size spends every second | (2,2), c_w=C=1000, m=20, p=1; d ∈ {1,10,60,300,599,600,601,900} | d=1: 40/40/40; d=10: 360/400/400; d=60–600: 1000/1000/1000; d=601: 1000/1040; d=900: 1000/2000 | ok (ratio 1.00 at d=1 and d≥60) |
| Bursts at the edges of saturation and age-out (B fires in (t0−d, t0] as A saturates, both refire around t0+W) | (1,4), p=1; d ∈ {5,30,60,300} | 140/400, 940/1000, 1000/1000, 1000/1000 | ok |
| A refills exactly as B's relayed spends age out on A | (4,4); d ∈ {10,60,300} | 0 overshoot (A saturates from its own spends; attempt ineffective) | ok |
| Depth 2, **workers and sub-workers spending at the same time** | (2,2), c_w=500; d ∈ {1,30,300}; p=1 and p=0.5 | 40/80, 13/58, 1000/1000, 623/689, 1000/1000, 1000/1000 | ok |
| m large relative to C | m=C=1000 at p=0.02; m=500 sent every second | up to 796/996; 1000/1000 | ok |
| Many workers | (10,10), c_w=300, p=0.5; d ∈ {1,10,60} | 31/89, 584/647, 1000/1000 | ok |
| B spends before A within each second | d ∈ {1,30} | 17/38, 346/438 | ok |
| d ≥ W (relayed entries arrive expired) | d ∈ {600,601,900} | above | ok (each side is effectively alone; overshoot = C) |
| Random fuzz, 400 runs | W∈{10..100}, C∈{50..500}, c_w∈[1,C], n∈[1,6]², m∈[1,C], p∈{.1,.5,1}, d∈{0,1,2,3,W/2,W−1,W,W+1,2W}, depth 1/2 with and without concurrent workers, both orders, signer fixed or not, random or burst amounts | max ratio 1.000 | **0 failures** of H1, H2, tight, H3 or H4; 0 ground-truth mismatches |

Positive controls (mutated relay, to check the checks can fail): a relay 30 s later than d fails H2 in 15 of 15 runs; a relay that writes only into the leaf fails H2 in 15 of 15; a relay 5 s late fails in only 7 of 15 (see D3).

## 5. Fresh seeds 100–109 (all 112 cells, 1,120 runs, unmodified `run()`)

| Hypothesis | Pass |
|---|---|
| H1 | 1120/1120 (160/160 d=0 runs have overshoot 0) |
| H2 | 1120/1120 (max ratio by d: 1: 0.33, 5: 0.80, 10: 0.83, 30: 0.89, 60: 0.985, 300: 1.00) |
| H3 | 1120/1120 (max overshoot/bound_caps = 0.50) |
| H4 | 1120/1120 |
| H5 | PASS (but see D2) |

For the record: the 1.0 H4 criterion (`overshoot_after_last ≤ d`) fails in 82/160, 154/160 and 160/160 runs at d = 1, 5 and ≥10.

## 6. Statistical adequacy

- H2 is deterministic: given the model it is a theorem (proof in item 4). Twenty seeds per cell cannot turn "not observed to fail" into a probability statement about the *design*, because nothing about the design is random. The seeds only sample spend patterns, and that sampling is biased away from the worst case: independent Bernoulli traffic, and a signer defect that suppresses spending after saturation.
- As a sampling statement, 0 failures in n runs gives a 95% upper bound of about 3/n on the per-run failure rate under *this traffic distribution* (the rule of three). The sweep has 1,920 d>0 runs, but only **960 are distinct** (D2), giving ≈0.31%. Per cell it is 3/20 = 15%. That says nothing about adversarial traffic.
- A proper statement of confidence has four parts:
  1. A written proof of the bound under explicit assumptions: honest, lossless, in-order, on-time relay; shared clock; each branch spending on one ledger only; only `transfer` ops.
  2. Evidence that the implementation matches the model: the §6 invariants, the log-based ground truth, and **positive controls** (mutants that must fail, as in `mutation.py`).
  3. Evidence that the bound is **tight**: adversarial runs reach ratio 1.0.
  4. A coverage criterion, e.g. "in each d>0 cell at least one run reaches ≥0.9 of the bound, or the cell is reported as under-stressed".
- **Pass criteria.** 100% is the right threshold for deterministic bounds, but:
  - H2 should be checked per event against the tight bound (overshoot at t ≤ remote unseen at the latest spend at or before t), not against a maximum over the whole run.
  - H3 should compare against min(C, remote caps). The current `max(n_a,n_b)·c_w` can be 4× C and can never fail.
  - H4 (1.1) is implied by the §6 window invariant plus amounts ≥ 1, so it adds no evidence. The 1.0 H4 was dropped after it failed on a smoke run. The reason given (window drain versus relay lag) is sound, but it should be reported as a revised hypothesis, not a pass.
  - H5 needs a design in which depth 2 differs from depth 1.
  - H1 under the 1.1 semantics failed during the full sweep and the model was then changed (1.2). The change log discloses this, and RESULTS.md must too.

## 7. Threats to validity missing from §8

1. **Split-brain branch caps.** Every account exists on both ledgers with the same signer, and an intra-tree move is not a spend. So the root (or any ancestor) on the "wrong" ledger can fund a worker's mirror account, and that worker can then spend `c_w` on each ledger. I reproduced 300 + 300 against a cap of 300 in one second. "A remote branch never exceeds its own local caps" holds *per ledger*. It relies on an unenforced assumption that each branch spends on one ledger only, which §2.2 states as model setup, not as a protocol rule.
2. **Clock skew is not just extra delay.** The relay carries the *source's* timestamp, and the receiving ledger ages entries out by that timestamp. A source whose clock runs δ behind makes its spends expire δ early on the receiver, which opens extra headroom there. That is a window-membership error, not a delay, and a larger d does not cover it.
3. **The enclave-side signer diverges from the ledger (D1).** It changes the traffic after saturation, so results measure a system whose leaves are throttled by phantom spends.
4. **Only `transfer` is exercised.** Channel deposits, pool joins and streams (`ledger.py:179-222`) also go through `_authorise`, but with extra failure points after recording, and off-chain updates the relay never sees.
5. **The relay writes straight into `SpendWindow`.** It bypasses the ledger API. In a real deployment the relay message would be a ledger operation with its own validation, cost and possible failure. The simulation models an ideal state-injection channel.
6. **Fixed A-before-B order within each second.** All d=1 overshoot comes from this ordering, not from delay.
7. **Short horizon.** 1,800 s = 3W, so steady-state or resonance effects over many windows are only lightly sampled.
8. **Coarse amounts.** m=20 ≪ C, so overshoot granularity is small and near-cap behaviour with m close to C is not in the sweep. My adversarial runs cover it, and it still passes.
9. **Library issues that bear on the whitepaper claim:** the escalation bypass of ancestors' `per_tx_max`, and `within()` ignoring `window_secs` and `escalation`.
10. **Process.** The hypotheses (H4, `bound_caps`) were revised after a smoke run, and the d=0 model was revised after the full sweep began. All of this is disclosed, but it is post-hoc and should be stated as such in RESULTS.md.

---

## Defects

1. **D1, should-fix.** A refused spend charges the enclave signer's window, violating §2.4 "no effect"; `refused` counts signer and ledger refusals together; overshoot magnitudes are understated (e.g. 599→674). Location: `foliant/agent.py:39-44`, `foliant/accounts.py:200-205`, used at `sim/crosschain.py:138-142`. Fix: in the simulation, snapshot `ag.signer.window.entries` before `transfer` and restore it when a `PolicyViolation` comes from the ledger; count signer and ledger refusals separately. In the library, record into the signer window only after the ledger accepts, or give `sign_payment` a two-phase or rollback API. Rerun the sweep and report the corrected magnitudes.
2. **D2, should-fix.** Depth-2 cells are exact duplicates of depth-1 cells (1,120/1,120 identical). H5 is vacuous, and the effective sample size is half the stated one. Location: `sim/crosschain.py:73-78`. Fix: at depth 2, make workers and sub-workers both spend (as `extra_spenders` does in the harness), and/or give sub-workers a cap below c_w. Redefine H5 as a comparison, e.g. "per-seed overshoot at depth 2 ≤ H2 bound, with the depth-1 and depth-2 distributions reported".
3. **D3, should-fix.** H2 compares against a whole-run maximum and has low sensitivity: a relay 5 s late passes in 8 of 15 runs. Location: `sim/crosschain.py:111-114`, `:160-161`, `:200`. Fix: at each applied spend, record U = remote amount undelivered (equal to the (s−d, s] sum); at each t, assert `overshoot(t) ≤ U(latest spend event ≤ t)`. Keep `bound_rate` as the reported "rate × delay" figure.
4. **D4, should-fix.** Overshoot is structurally ≤ min(C, remote caps), because each ledger caps its own local spends at C. So `bound_caps` = max(n_a,n_b)·c_w can never bind when it is ≥ C, which is true in 3 of the 4 (c_w, n) combinations (up to 4C). Max observed overshoot/bound_caps is 0.50, so H3 cannot fail in those cells. Location: `sim/crosschain.py:187`. Fix: use `min(C, remote-side caps)` for the side that spent last, or at least `min(C, max(n_a,n_b)·c_w)`; also check `overshoot ≤ Σcaps − C`.
5. **D5, note.** The H2 flag is computed for d=0 rows and included in the H2 pass count. Location: `sim/crosschain.py:200`, `:239-241`. Fix: `"H2": P.d == 0 or …`, and report H2 over d>0 rows only.
6. **D6, note.** There are 1,801 spend seconds, not 1,800. Location: `sim/crosschain.py:133`. Fix: `if step < P.duration`.
7. **D7, should-fix.** §6 invariant 3 checks leaves only, after the fact, from the harness's own list. An invariant failure kills the whole sweep with no output, and `-O` disables the checks. Location: `sim/crosschain.py:163-183`, `:234-238`. Fix: at each spend, before `transfer`, assert for every account in the local lineage that `window.spent(t,W) + amt ≤ per_window_max` whenever the ledger accepts. Raise an `InvariantError` that is caught per run and recorded in the row. Write CSV rows as runs finish.
8. **D8, note.** The docstring cites spec v1.0. Location: `sim/crosschain.py:1`. Fix: cite v1.2.
9. **D9, note.** The spec says ids depend on "owner key, signer key and salt"; child ids are `hash(parent, signer, salt)`. Location: `SPEC.md` §2.2 vs `foliant/accounts.py:165`. Fix: correct the spec wording.
10. **D10, note.** H4 (1.1) adds no evidence beyond the §6 window invariant, and the 1.0 H4 failed. Location: `SPEC.md` §4 and the change log. Fix: say so in RESULTS.md; report `overshoot_after_last` as a measured drain time, not as a pass.
11. **D11, note (library; should-fix for the whitepaper).** A child's escalation key bypasses every ancestor's `per_tx_max`; `within()` ignores `escalation` and `window_secs`. Location: `foliant/ledger.py:133`, `foliant/accounts.py:89-105`. Fix: pass `escalated` only to the account whose escalation key signed, or require every ancestor on the chain to have escalation authority. Add `window_secs` and `escalation` checks to `within()`.
12. **D12, note (library).** Windows are recorded before later failure points (`_move`, "channel exists"), leaving phantom entries. Location: `foliant/ledger.py:175-176`, `:182-185`. Fix: validate first, then record and move together, or roll back on exception.
13. **D13, note.** The code does not produce `RESULTS.md` or `overshoot.png` (§7). Location: `sim/crosschain.py:229-245`. Fix: add a report step, or state that they are produced by hand.
14. **D14, note (threat).** Split-brain: a branch funded on both ledgers gets its cap once per ledger. Location: `SPEC.md` §2.2, §8. Fix: add it to §8; in the protocol, bind each account to a single home ledger, or make remote mirrors unfundable.

## Overall conclusion

The results support the claim only in a narrower form than stated. What they support: with an honest, lossless, in-order relay that delivers exactly d seconds late, a shared clock, `transfer` as the only operation, and each branch spending on only one ledger, the crew's committed value never exceeds C by more than the remote side applied in the d-second interval ending at the last local spend. That quantity is at most the maximum over any d-interval (`bound_rate`), and at most min(C, the remote side's local caps). This follows from a short proof. The simulation implements it faithfully: the ground truth, the relay and the tree windows all checked out, including against the ledgers' own logs. No violation appeared in 2,240 original runs, 1,120 fresh-seed runs, 32 adversarial runs or 400 fuzz runs, and adversarial traffic reaches the bound exactly, so the check is not vacuous.

The evidence is weaker than the pass counts suggest:
- Half the sweep duplicates the other half, and H5 is untested.
- The H2 comparison uses a whole-run maximum, which misses a relay that is slightly late.
- H3's bound cannot bind in three quarters of the cells.
- The enclave-signer defect throttles traffic after saturation, so the reported overshoot magnitudes are understated by up to about 12%.

The clause "never by more than its own local caps" holds only per ledger. Nothing in the library stops a branch from being funded and spending on both ledgers, which doubles its effective cap. RESULTS.md should state the claim with these assumptions, report corrected magnitudes after D1–D4 are fixed, and label H2 as a proven bound confirmed by simulation, not as a statistical finding.


---

# Appendix B: independent audit 2 (unedited)

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
