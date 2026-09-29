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
