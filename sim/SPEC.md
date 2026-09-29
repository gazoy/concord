# Cross-chain budget tree: simulation specification

Version 1.3, 29 September 2026. Written before any simulation code. Changes after
implementation begins are recorded in the change log at the end, with reasons.

## 1. Question

Whitepaper §6.3a enforces a hierarchical budget because one ledger sees every
branch of the tree. If the branches of one crew hold accounts on two ledgers
that learn of each other's spends only after a relay delay, by how much and for
how long can the crew's total committed value exceed the root's cap?

## 2. Model

### 2.1 Ledgers

Two instances of the reference `Ledger` (`foliant/ledger.py`), unmodified,
called A and B. Time is discrete in seconds; both ledgers share one clock.

### 2.2 Tree

One crew, mirrored on both ledgers with identical account ids (a root id is
deterministic in owner key, signer key and salt; a child id in parent id,
signer key and salt):

- root R, policy `per_window_max = C`, `window_secs = W`, `per_tx_max ≥ C`.
- N_A workers delegated from R with accounts funded on A; N_B workers funded on B.
  Each worker has `per_window_max = c_w ≤ C`.
- Optionally one further level (depth 2): each worker delegates one sub-worker
  on the same ledger with `per_window_max = c_w / 2`, and **both** the worker
  and the sub-worker spend, concurrently.

A worker spends only on the ledger where it is funded. Spends are transfers to
an address outside the tree, so they pass through `_authorise` and are recorded
in every ancestor's window on the local ledger.

### 2.3 Relay

Each ledger keeps an outbox of (account chain, timestamp, amount) for every
spend it has applied. A relay delivers each outbox entry to the other ledger
exactly `d` seconds after it was applied, in order, by recording (timestamp,
amount) into the windows of the same accounts on the receiving ledger. Nothing
else is exchanged. The relay is honest, lossless and in-order; `d` is constant
within a run.

Consequence: at time t, ledger A's view of the root's window equals A-local
spends up to t plus B-local spends up to t − d.

### 2.4 Spend process

Each worker attempts a spend at each second with probability p, of an amount
drawn uniformly from [1, m]. An attempt that the local ledger refuses
(`PolicyViolation`) is counted as refused and has no effect, on the ledger or
on the leaf's signer. Attempts the leaf's own signer refuses before reaching
the ledger are counted separately (`refused_signer`). All randomness
comes from one seeded generator; a run is fully determined by its seed and
parameters.

### 2.5 Ground truth

The harness records every applied spend with its true timestamp. The true
committed value of the tree at time t is the sum of applied spends with
timestamp in (t − W, t]. This is computed outside both ledgers.

## 3. Metrics

Per run:

- `overshoot_max`: max over t of (true committed − C), floored at 0.
- `overshoot_secs`: number of seconds t at which true committed > C.
- `refused`: number of refused attempts.
- `bound_rate`: max over t of (sum of B-side applied spends in (t − d, t]) as
  seen from A, and the symmetric quantity for B. This is the "rate × delay"
  quantity the hypothesis compares against.
- `bound_caps`: min(C, the larger side's sum of leaf `per_window_max`).
  Overshoot can originate from either side, and each ledger caps its own local
  spends at C, so this is the tightest structural bound available per run.
- `slack_caps`: the sum of all leaf caps on both sides minus C. Overshoot can
  never exceed this either, and in cells where the sum of caps is below C it
  is the binding bound.
- `tight_violations`: number of seconds t at which overshoot(t) exceeded the
  amount the other side had applied, unseen, in the d seconds up to the most
  recent applied spend at or before t. This is the per-event form of the H2
  bound.
- `h4_violations`: number of seconds t at which `seen_by_all(t) ≥ C` and a spend
  was nonetheless applied.

## 4. Hypotheses and pass criteria

H1 (safety with zero delay). With d = 0, `overshoot_max = 0` in every run.
Pass: 100% of runs.

H2 (rate-delay bound). For d > 0: (a) `overshoot_max ≤ bound_rate` in every
run, where `bound_rate` is the largest amount the remote side applied inside
any window of length d; and (b) `tight_violations = 0`, the per-event form:
at every second, overshoot is at most what the other side had applied unseen
at the time of the last spend. Pass: 100% of d > 0 runs. (b) is the
sensitive check; (a) is the reported figure. H2 is evaluated over d > 0 runs
only. Failure means overshoot compounds beyond what the remote side could
have committed unseen, which would indicate a defect in the design or the
simulation.

H3 (cap bound). `overshoot_max ≤ bound_caps` and `overshoot_max ≤ slack_caps`
in every run: a remote branch can never exceed its own local caps, whatever
the delay, and the tree can never exceed the sum of its leaf caps. Pass: 100%
of runs. Note that where the larger side's caps are ≥ C the first bound
cannot bind; the second can.

H4 (refusal lag ≤ d). Let `seen_by_all(t)` be the sum of applied spends with
timestamp in (t − W, t − d]: every spend both ledgers have been told about and
that is still inside the window at t. If `seen_by_all(t) ≥ C` then no spend is
applied at t on either ledger. Pass: 100% of runs. (Revised in 1.1; see change
log.)

H5 (depth independence). With workers and sub-workers spending concurrently,
H2 (both parts) holds in every depth-2 run. Pass: 100% of depth-2, d > 0
runs. The depth-1 and depth-2 overshoot distributions are reported side by
side.

Any hypothesis failing in any run is a reportable finding; the report states
which, the seed, and the parameters.

## 5. Parameter sweep

| Parameter | Values |
| --- | --- |
| d (relay delay, s) | 0, 1, 5, 10, 30, 60, 300 |
| C (root cap) | 1000 |
| c_w (worker cap) | 300, 1000 |
| N_A, N_B | (2, 2), (1, 4) |
| p (attempt probability per second) | 0.05, 0.5 |
| m (max spend) | 20 |
| W (window, s) | 600 |
| depth | 1, 2 |
| duration (s) | 1800 |
| seeds | 20 per cell |

Total cells: 7 × 2 × 2 × 2 × 2 = 112; runs: 2,240.

## 6. Invariants checked every second in every run

- Supply on each ledger is conserved (`total_supply` constant).
- On each ledger, every account's recorded window equals the sum of its
  subtree's spends that ledger has been told about. (Exercises the tree code,
  not the relay.)
- At the time of every accepted spend, for every account in the leaf's local
  lineage (leaf, worker, root), that account's local window plus the amount
  was within its `per_window_max`. Checked before the ledger is called and
  compared with the ledger's decision.

An invariant failure ends that run, is recorded in its CSV row, fails all its
hypotheses, and does not stop the sweep. Rows are written as runs finish.

## 7. Outputs

- `sim/results.csv`: one row per run with parameters, seed and all metrics.
- `sim/RESULTS-auto.md`: generated by the code: pass/fail per hypothesis and
  the worst case per delay.
- `sim/RESULTS.md`: written by hand: the claim as supported, the assumptions,
  what changed during the study and why, and the independent audit reports
  appended unedited.
- `sim/overshoot.png`: `overshoot_max` against d, per (p, c_w), with the H2
  bound drawn.

## 8. Threats to validity

- The relay is honest and in-order. A lying, dropping or reordering relay is
  out of scope; this simulation cannot say anything about it.
- Spend attempts are independent per second; bursty real traffic could reach
  the rate-delay bound more often but cannot exceed it if H2 holds.
- Both ledgers share one clock. Clock skew adds to effective delay and is
  covered by choosing a larger d.
- The window is a sliding sum; Tempo-style fixed periods would give different
  edge behaviour. Out of scope.
- Two ledgers only. With k ledgers the bound becomes a sum over remote sides;
  the two-ledger result generalises additively if H2 holds, but this is not
  tested here.
- **Each branch spends on one ledger only.** This is model setup, not a
  protocol rule. Every account exists on both ledgers with the same signer, and
  a move inside the tree is not a spend, so an ancestor on the "wrong" ledger
  could fund a worker's mirror and that worker could then spend its cap on
  each ledger: 300 + 300 against a cap of 300 (found by the audit). The
  "never more than its own local caps" clause therefore holds per ledger. A
  protocol that spans ledgers must bind each account to one home ledger or
  make remote mirrors unfundable; this simulation does not model that.
- **Clock skew is not only extra delay.** The relay carries the source's
  timestamp and the receiver ages entries out by it, so a source clock that
  runs slow makes its spends expire early on the receiver, opening headroom
  there. That is a window-membership error a larger d does not cover.
- Only `transfer` is exercised. Channel deposits, pool joins and streams pass
  through the same check but have other failure points and off-chain updates
  the relay never sees.
- The relay writes directly into the receiving ledger's windows, an ideal
  state-injection channel; a real relay message would be a ledger operation
  with its own validation and cost.
- Within a second, A's spenders act before B's. All overshoot at d = 1 comes
  from this ordering, not from delay.
- Horizon is 3W; long-run or resonance effects across many windows are only
  lightly sampled. Amounts are small relative to C (m = 20); the audit's
  adversarial runs cover m near C.
- The hypotheses H4 and the definition of `bound_caps` were revised after a
  smoke run, and the d = 0 model after the full sweep began. Every revision is
  in the change log; the results should be read as confirming a bound that was
  then stated correctly, not as a pre-registered statistical test.

## 9. Independent audit

A separate reviewer who did not write the code receives this specification,
the code and the results, and is asked to: check the model matches this
specification; look for defects in the relay, the ground truth, the metrics and
the pass criteria; rerun with fresh seeds; and try to construct a parameter set
or spend pattern that breaks H2. Their report is appended to RESULTS.md
unedited.

## Change log

- 1.0: initial.
- 1.1 (after first smoke run): H4 rewritten. The 1.0 wording measured
  `overshoot_secs` after the last spend and required it to be ≤ d, which
  conflates two things: the relay catching up (d seconds) and the sliding
  window draining (up to W seconds). True committed value legitimately stays
  above C until old spends age out, whatever the relay does. The property the
  relay is responsible for is that refusal recovers within d, which is what 1.1
  states. The 1.0 metric is kept in the CSV as `overshoot_after_last` for the
  record but is no longer a pass criterion. Also: `bound_caps` clarified as the
  max over sides, since overshoot can come from either.
- 1.1: relay delivery runs both before and after each second's spends so that
  d = 0 means "seen within the same second"; the 1.0 code delivered only before,
  which made d = 0 behave as d = 1 and was caught by the §6 window invariant.
- 1.2 (after the full sweep began): at d = 0 the 1.1 code still let a spend on
  A and a spend on B in the same simulated second each pass unseen by the
  other, producing overshoot of up to n × m at d = 0. That is sub-second
  concurrency, not zero delay, and H2 already bounds it; but H1 is meant to be
  the single-ledger sanity case, so d = 0 now delivers after every individual
  spend and is truly synchronous. Sub-second concurrency is represented by
  d = 1. The 1.1 behaviour is recorded here because it is itself a finding:
  "zero relay delay" in a real deployment is never atomic, and the d = 1 cell
  is the honest lower bound for a live system.
- 1.3 (after the first independent audit, sim/AUDIT-1.md): fourteen findings,
  applied as follows. Library: a refused spend no longer stays charged on the
  enclave signer's window (D1); an escalation co-signature lifts only the
  signing account's `per_tx_max`, never an ancestor's (D11); funds and
  duplicate-object checks run before any window is recorded (D12).
  Simulation: depth 2 now has workers and sub-workers spending concurrently
  with the sub-worker at half cap, so depth-2 runs are no longer identical to
  depth-1 runs and H5 is a real test (D2); H2 gains the per-event tight bound
  (D3); `bound_caps` is min(C, larger side's caps) and `slack_caps` is added
  (D4); H2 is evaluated over d > 0 only (D5); spends run for exactly
  `duration` seconds (D6); the per-account invariant is checked at spend time
  for the whole local lineage, an invariant failure fails the run and not the
  sweep, and rows are written incrementally (D7); the docstring cites the
  current spec (D8); child ids are described correctly (D9); H4's status is
  stated in RESULTS.md (D10); `RESULTS-auto.md` and `overshoot.png` are
  produced by the code (D13); split-brain funding and clock skew added to §8
  (D14, threats 1-2). `window_secs` and `escalation` are deliberately not
  compared by `Policy.within`, for the reasons now in its docstring.
