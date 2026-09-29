"""Cross-chain budget tree simulation. Implements sim/SPEC.md v1.0 exactly; any
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
        leaf = w
        if P.depth == 2:
            leaf = w.delegate(KeyPair.from_seed(f"{label}-sub-signer".encode()), wpol,
                              fund=10 ** 6 if funded else 0, asset=ASSET, salt=100 + i)
        if funded:
            spenders.append(leaf)
    return Side(name, L, root, spenders)


def chain_ids(side: Side, agent: Agent) -> list[str]:
    return [a.id for a in side.ledger.lineage(agent.account)]


def run(P: Params) -> dict:
    rng = random.Random(P.seed)
    a_labels = {f"a{i}" for i in range(P.n_a)}
    b_labels = {f"b{i}" for i in range(P.n_b)}
    A = build_side("A", P, a_labels)
    B = build_side("B", P, b_labels)
    sides = {"A": A, "B": B}
    other = {"A": B, "B": A}
    supply0 = {s.name: s.ledger.total_supply(ASSET) for s in sides.values()}

    applied: list[Spend] = []  # ground truth
    refused = 0
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
        if step <= P.duration:
            for s in sides.values():
                for ag in s.spenders:
                    if rng.random() < P.p:
                        amt = rng.randint(1, P.m)
                        try:
                            ag.transfer(OUTSIDE, ASSET, amt)
                        except PolicyViolation:
                            refused += 1
                            continue
                        sp = Spend(t, amt, chain_ids(s, ag), s.name)
                        applied.append(sp)
                        s.outbox.append(sp)
                        last_spend_t = t
                        applied_this_second += 1
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

    # per-account own-cap invariant: every applied spend was within its leaf's local window
    # (guaranteed by the ledger; checked here by reconstruction)
    for side_name in sides:
        by_leaf: dict[str, list[Spend]] = {}
        for sp in applied:
            if sp.side == side_name:
                by_leaf.setdefault(sp.chain[0], []).append(sp)
        for leaf, sps in by_leaf.items():
            for i, sp in enumerate(sps):
                in_win = sum(x.amount for x in sps[: i + 1] if sp.t - P.W < x.t <= sp.t)
                assert in_win <= P.c_w, "leaf exceeded its own cap"

    # H3 bound: the remote side's workers can never exceed their own caps. The remote side
    # relative to A is B and vice versa; overshoot can come from either, so take the max.
    bound_caps = max(P.c_w * P.n_a, P.c_w * P.n_b)

    return {
        **P.__dict__,
        "applied": len(applied),
        "refused": refused,
        "overshoot_max": overshoot_max,
        "overshoot_secs": overshoot_secs,
        "overshoot_after_last": overshoot_after_last,
        "bound_rate": bound_rate,
        "bound_caps": bound_caps,
        "h4_violations": h4_violations,
        "H1": (P.d > 0) or overshoot_max == 0,
        "H2": overshoot_max <= bound_rate,
        "H3": overshoot_max <= bound_caps,
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=str(Path(__file__).with_name("results.csv")))
    args = ap.parse_args()
    rows = sweep(args.quick)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    for h in ("H1", "H2", "H3", "H4"):
        fails = [r for r in rows if not r[h]]
        print(f"{h}: {'PASS' if not fails else 'FAIL'} ({len(rows) - len(fails)}/{len(rows)})")
        for r in fails[:5]:
            print("   ", {k: r[k] for k in ("d", "c_w", "n_a", "n_b", "p", "depth", "seed", "overshoot_max", "bound_rate", "bound_caps")})
    h5 = all(r["H2"] for r in rows if r["depth"] == 2)
    print(f"H5: {'PASS' if h5 else 'FAIL'}")


if __name__ == "__main__":
    main()
