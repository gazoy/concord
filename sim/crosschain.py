"""Cross-chain budget tree simulation. Implements sim/SPEC.md v1.3 exactly; any
deviation is a defect. Run `python sim/crosschain.py` for the full sweep or
`python sim/crosschain.py --quick` for a smoke test.
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foliant import Agent, KeyPair, Ledger, Policy
from foliant.errors import PolicyViolation

ASSET = "U"
OUTSIDE = KeyPair.from_seed(b"outside").address
T0 = 1_700_000_000


@dataclass
class Params:
    d: int
    C: int
    c_w: int
    n_a: int
    n_b: int
    p: float
    m: int
    W: int
    depth: int
    duration: int
    seed: int


@dataclass
class Spend:
    t: int
    amount: int
    chain: list[str]  # account ids, leaf first
    side: str  # "A" or "B"


@dataclass
class Side:
    """One ledger plus the mirrored tree and the agents funded here."""
    name: str
    ledger: Ledger
    root: Agent
    spenders: list[Agent]
    outbox: deque = field(default_factory=deque)  # Spend awaiting relay


def build_side(name: str, P: Params, fund_here: set[str]) -> Side:
    """Build the mirrored tree on one ledger. `fund_here` names the worker labels
    whose accounts are funded (and therefore spend) on this ledger. Every account
    exists on both ledgers with the same id; only funding differs."""
    L = Ledger(now=T0)
    root_policy = Policy(per_tx_max=P.C, per_window_max=P.C, window_secs=P.W)
    root = Agent(L, KeyPair.from_seed(b"root-owner"), KeyPair.from_seed(b"root-signer"), root_policy)
    L.mint(root.account.address, ASSET, 10 ** 9)
    spenders: list[Agent] = []
    labels = [f"a{i}" for i in range(P.n_a)] + [f"b{i}" for i in range(P.n_b)]
    wpol = Policy(per_tx_max=P.c_w, per_window_max=P.c_w, window_secs=P.W)
    for i, label in enumerate(labels):
        funded = label in fund_here
        w = root.delegate(KeyPair.from_seed(f"{label}-signer".encode()), wpol,
                          fund=10 ** 6 if funded else 0, asset=ASSET, salt=i)
        if funded:
            spenders.append(w)
        if P.depth == 2:
            # the sub-worker has half the worker's cap; both spend, concurrently
            sub = w.delegate(KeyPair.from_seed(f"{label}-sub-signer".encode()),
                             Policy(per_tx_max=P.c_w, per_window_max=P.c_w // 2, window_secs=P.W),
                             fund=10 ** 5 if funded else 0, asset=ASSET, salt=100 + i)
            if funded:
                spenders.append(sub)
    return Side(name, L, root, spenders)


def chain_ids(side: Side, agent: Agent) -> list[str]:
    return [a.id for a in side.ledger.lineage(agent.account)]


def run(P: Params) -> dict:
    try:
        return _run(P)
    except AssertionError as e:
        return {**P.__dict__, "invariant_error": str(e), "H1": False, "H2": False, "H3": False, "H4": False}


def _run(P: Params) -> dict:
    rng = random.Random(P.seed)
    a_labels = {f"a{i}" for i in range(P.n_a)}
    b_labels = {f"b{i}" for i in range(P.n_b)}
    A = build_side("A", P, a_labels)
    B = build_side("B", P, b_labels)
    sides = {"A": A, "B": B}
    other = {"A": B, "B": A}
    supply0 = {s.name: s.ledger.total_supply(ASSET) for s in sides.values()}

    applied: list[Spend] = []  # ground truth
    refused = 0            # refused by the ledger (an ancestor's policy)
    refused_signer = 0     # refused by the leaf's own signer before reaching the ledger
    tight_violations = 0   # overshoot(t) > remote unseen at the last spend at or before t
    unseen_at_last_spend = 0
    invariant_error = ""
    overshoot_max = 0
    overshoot_secs = 0
    last_spend_t = T0
    overshoot_after_last = 0
    bound_rate = 0
    h4_violations = 0

    def true_committed(t: int) -> int:
        return sum(s.amount for s in applied if t - P.W < s.t <= t)

    def seen_by_all(t: int) -> int:
        return sum(s.amount for s in applied if t - P.W < s.t <= t - P.d)

    def remote_in_delay_window(t: int, side_name: str) -> int:
        """Amount the *other* side applied in (t - d, t]: unseen by `side_name` at t."""
        o = other[side_name].name
        return sum(s.amount for s in applied if s.side == o and t - P.d < s.t <= t)

    for step in range(P.duration + P.d + 1):
        t = T0 + step
        for s in sides.values():
            s.ledger.now = t
        # 1. deliver relay entries due at t (applied at t - d); run again after this
        #    second's spends so that d = 0 means "seen within the same second"
        def deliver() -> None:
            for s in sides.values():
                dest = other[s.name]
                while s.outbox and s.outbox[0].t + P.d <= t:
                    sp = s.outbox.popleft()
                    for acct_id in sp.chain:
                        dest.ledger.accounts[acct_id].window.record(sp.t, sp.amount)
        deliver()
        # 2. spend attempts (none after `duration`; the tail lets the relay drain)
        seen_before_spends = seen_by_all(t)
        applied_this_second = 0
        if step < P.duration:
            for s in sides.values():
                for ag in s.spenders:
                    if rng.random() < P.p:
                        amt = rng.randint(1, P.m)
                        chain = chain_ids(s, ag)
                        # §6 invariant 3, at spend time, against the local view of every account
                        # in the lineage: if the ledger accepts, no local window was over its cap
                        room = all(s.ledger.accounts[c].window.spent(t, P.W) + amt
                                   <= s.ledger.accounts[c].policy.per_window_max for c in chain)
                        try:
                            ag.transfer(OUTSIDE, ASSET, amt)
                        except PolicyViolation as e:
                            # was it the signer or the ledger? the signer raises before apply()
                            if ag.signer.window.spent(t, P.W) + amt > ag.signer.policy.per_window_max:
                                refused_signer += 1
                            else:
                                refused += 1
                            continue
                        if not room:
                            raise AssertionError("ledger applied a spend a local window should have refused")
                        sp = Spend(t, amt, chain, s.name)
                        # tight bound: what the other side applied in (t - d, t], unseen here now
                        unseen_at_last_spend = remote_in_delay_window(t, s.name)
                        applied.append(sp)
                        s.outbox.append(sp)
                        last_spend_t = t
                        applied_this_second += 1
                        deliver()  # d = 0 is synchronous: the other ledger sees this spend at once
        if seen_before_spends >= P.C and applied_this_second:
            h4_violations += 1
        deliver()
        # 3. metrics
        tc = true_committed(t)
        over = max(0, tc - P.C)
        if over:
            overshoot_secs += 1
            overshoot_max = max(overshoot_max, over)
            if t > last_spend_t:
                overshoot_after_last += 1
            if over > unseen_at_last_spend:
                tight_violations += 1
        for name in sides:
            bound_rate = max(bound_rate, remote_in_delay_window(t, name))
        # 4. invariants (§6)
        for s in sides.values():
            assert s.ledger.total_supply(ASSET) == supply0[s.name], "supply not conserved"
        # window of every account == subtree spends this ledger knows of
        for s in sides.values():
            known = [sp for sp in applied if sp.side == s.name or sp.t + P.d <= t]
            for acct in s.ledger.accounts.values():
                expect = sum(sp.amount for sp in known if acct.id in sp.chain and t - P.W < sp.t <= t)
                got = acct.window.spent(t, P.W)
                assert got == expect, f"{s.name} {acct.id[:6]} window {got} != known subtree {expect}"

    # H3 bound: overshoot is at most what the remote side could commit unseen, which its
    # own local caps limit, and each ledger caps its own local spends at C. Overshoot can
    # originate from either side, so the bound is min(C, larger side's caps); reported with
    # the sum-of-caps slack too.
    side_caps = {"A": P.c_w * P.n_a * (1.5 if P.depth == 2 else 1), "B": P.c_w * P.n_b * (1.5 if P.depth == 2 else 1)}
    bound_caps = int(min(P.C, max(side_caps.values())))
    slack_caps = int(sum(side_caps.values()) - P.C)

    return {
        **P.__dict__,
        "applied": len(applied),
        "refused": refused,
        "refused_signer": refused_signer,
        "overshoot_max": overshoot_max,
        "overshoot_secs": overshoot_secs,
        "overshoot_after_last": overshoot_after_last,
        "bound_rate": bound_rate,
        "bound_caps": bound_caps,
        "slack_caps": slack_caps,
        "h4_violations": h4_violations,
        "tight_violations": tight_violations,
        "invariant_error": invariant_error,
        "H1": (P.d > 0) or overshoot_max == 0,
        "H2": P.d == 0 or (overshoot_max <= bound_rate and tight_violations == 0),
        "H3": overshoot_max <= bound_caps and overshoot_max <= max(0, slack_caps),
        "H4": h4_violations == 0,
    }


def sweep(quick: bool) -> list[dict]:
    ds = [0, 1, 5, 10, 30, 60, 300]
    cells = []
    for d in ds:
        for c_w in (300, 1000):
            for n_a, n_b in ((2, 2), (1, 4)):
                for p in (0.05, 0.5):
                    for depth in (1, 2):
                        cells.append((d, c_w, n_a, n_b, p, depth))
    seeds = range(2 if quick else 20)
    if quick:
        cells = cells[::9]
    rows = []
    for (d, c_w, n_a, n_b, p, depth) in cells:
        for seed in seeds:
            P = Params(d=d, C=1000, c_w=c_w, n_a=n_a, n_b=n_b, p=p, m=20, W=600, depth=depth,
                       duration=300 if quick else 1800, seed=seed)
            rows.append(run(P))
        print(f"cell d={d} c_w={c_w} n=({n_a},{n_b}) p={p} depth={depth}: "
              f"max overshoot {max(r['overshoot_max'] for r in rows[-len(seeds):])}", flush=True)
    return rows


FIELDS = ["d", "C", "c_w", "n_a", "n_b", "p", "m", "W", "depth", "duration", "seed", "applied", "refused",
          "refused_signer", "overshoot_max", "overshoot_secs", "overshoot_after_last", "bound_rate", "bound_caps",
          "slack_caps", "h4_violations", "tight_violations", "invariant_error", "H1", "H2", "H3", "H4"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=str(Path(__file__).with_name("results.csv")))
    args = ap.parse_args()
    rows: list[dict] = []
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sweep(args.quick):
            rows.append(r)
            w.writerow({k: r.get(k, "") for k in FIELDS})
            f.flush()
    summary = []
    for h, subset in (("H1", [r for r in rows if r["d"] == 0]), ("H2", [r for r in rows if r["d"] > 0]),
                      ("H3", rows), ("H4", rows), ("H5", [r for r in rows if r["depth"] == 2 and r["d"] > 0])):
        key = "H2" if h == "H5" else h
        fails = [r for r in subset if not r[key]]
        summary.append((h, len(subset) - len(fails), len(subset), fails))
        print(f"{h}: {'PASS' if not fails else 'FAIL'} ({len(subset) - len(fails)}/{len(subset)})")
        for r in fails[:5]:
            print("   ", {k: r.get(k) for k in ("d", "c_w", "n_a", "n_b", "p", "depth", "seed", "overshoot_max", "bound_rate", "bound_caps", "invariant_error")})
    write_report(rows, summary, Path(args.out).parent)


def write_report(rows: list[dict], summary, out_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ds = sorted({r["d"] for r in rows})
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for (p, c_w), mk in (((0.05, 300), "o"), ((0.05, 1000), "s"), ((0.5, 300), "^"), ((0.5, 1000), "D")):
        ys = [max((r["overshoot_max"] for r in rows if r["d"] == d and r["p"] == p and r["c_w"] == c_w), default=0) for d in ds]
        bs = [max((r["bound_rate"] for r in rows if r["d"] == d and r["p"] == p and r["c_w"] == c_w), default=0) for d in ds]
        ax.plot(ds, ys, marker=mk, label=f"max overshoot, p={p}, c_w={c_w}")
        ax.plot(ds, bs, linestyle=":", color=ax.lines[-1].get_color(), label=f"H2 bound (rate x delay), p={p}, c_w={c_w}")
    ax.set_xscale("symlog", linthresh=1)
    ax.set_xlabel("relay delay d (s)")
    ax.set_ylabel("units (root cap C = 1000)")
    ax.set_title("Cross-chain budget tree: worst overshoot vs relay delay")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(out_dir / "overshoot.png", dpi=150)
    lines = ["# Results", "", f"Runs: {len(rows)}. Generated by `python sim/crosschain.py`; spec sim/SPEC.md.", "",
             "| Hypothesis | Pass | Of |", "| --- | --- | --- |"]
    for h, ok, n, _ in summary:
        lines.append(f"| {h} | {ok} | {n} |")
    lines += ["", "## Worst case per delay", "", "| d | max overshoot | max bound_rate | max overshoot / bound | refused (ledger) | refused (signer) |", "| --- | --- | --- | --- | --- | --- |"]
    for d in ds:
        sub = [r for r in rows if r["d"] == d]
        mo = max(r["overshoot_max"] for r in sub)
        mb = max(r["bound_rate"] for r in sub)
        ratio = max((r["overshoot_max"] / r["bound_rate"] for r in sub if r["bound_rate"]), default=0)
        lines.append(f"| {d} | {mo} | {mb} | {ratio:.2f} | {sum(r['refused'] for r in sub)} | {sum(r['refused_signer'] for r in sub)} |")
    lines += ["", "![overshoot](overshoot.png)", ""]
    (out_dir / "RESULTS-auto.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
