# Cross-chain budget tree: simulation specification

Version 1.1, 29 September 2026. Written before any simulation code. Changes after
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

One crew, mirrored on both ledgers with identical account ids (ids are
deterministic in owner key, signer key and salt):

- root R, policy `per_window_max = C`, `window_secs = W`, `per_tx_max ≥ C`.
- N_A workers delegated from R with accounts funded on A; N_B workers funded on B.
  Each worker has `per_window_max = c_w ≤ C`.
- Optionally one further level: each worker delegates one sub-worker on the
  same ledger with `per_window_max = c_w`.

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
(`PolicyViolation`) is counted as refused and has no effect. All randomness
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
- `bound_caps`: the larger of the two sides' sums of worker `per_window_max`;
  overshoot can originate from either side.
- `h4_violations`: number of seconds t at which `seen_by_all(t) ≥ C` and a spend
  was nonetheless applied.

## 4. Hypotheses and pass criteria

H1 (safety with zero delay). With d = 0, `overshoot_max = 0` in every run.
Pass: 100% of runs.

H2 (rate-delay bound). For d > 0, `overshoot_max ≤ bound_rate` in every run,
where `bound_rate` is the largest amount the remote side applied inside any
window of length d. Pass: 100% of runs. Failure means overshoot compounds
beyond what the remote side could have committed unseen, which would indicate
a defect in the design or the simulation.

H3 (cap bound). `overshoot_max ≤ bound_caps` in every run: a remote branch can
never exceed its own local caps, whatever the delay. Pass: 100% of runs.

H4 (refusal lag ≤ d). Let `seen_by_all(t)` be the sum of applied spends with
timestamp in (t − W, t − d]: every spend both ledgers have been told about and
that is still inside the window at t. If `seen_by_all(t) ≥ C` then no spend is
applied at t on either ledger. Pass: 100% of runs. (Revised in 1.1; see change
log.)

H5 (depth independence). Adding the sub-worker level does not increase
`overshoot_max` beyond the H2 bound. Pass: H2 holds with depth 2.

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
- No applied spend exceeded its account's own `per_window_max` as measured on
  that account's local ledger at the time of the spend.

An invariant failure aborts the run and is reported.

## 7. Outputs

- `sim/results.csv`: one row per run with parameters, seed and all metrics.
- `sim/RESULTS.md`: pass/fail per hypothesis, the worst case per cell, and the
  figure.
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
