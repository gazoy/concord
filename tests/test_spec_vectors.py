"""Runs docs/spec/vectors.json against the Python reference (spending-policy.md §10).

Every vector kind is exercised: `check` and `within` directly on Policy; `window` on
SpendWindow (the exact §6.1 semantics); `tree` through the Ledger and Agent, so the
all-or-nothing walk up the tree (§4.2) and the internal-move rule (§4.3) are what is
tested, not a re-implementation; `x402-exact` through foliant.policy_x402.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from foliant.accounts import Policy, SpendWindow
from foliant.agent import Agent
from foliant.crypto import KeyPair, PublicKey, sign
from foliant.errors import PolicyViolation
from foliant.ledger import Ledger
from foliant.policy_x402 import check_exact

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "docs" / "spec" / "vectors.json").read_text())["vectors"]
ASSET = "USDC"


def policy_of(d: dict) -> Policy:
    """Spec wire form -> reference Policy, through the reference's own parser (validation and
    canonicalisation are the parser's job, spec §2, not the runner's)."""
    return Policy.from_wire(d)


def by_kind(kind: str):
    return [pytest.param(v, id=v["id"]) for v in VECTORS if v["kind"] == kind]


def outcome(fn) -> str:
    try:
        fn()
        return "ok"
    except PolicyViolation as e:
        return e.code


@pytest.mark.parametrize("v", by_kind("check"))
def test_check(v):
    p = policy_of(v["policy"])
    got = outcome(lambda: p.check(amount=int(v["amount"]), payee=v["payee"], now=v["now"],
                                  spent_in_window=int(v["spentInWindow"]), escalated=v["escalated"]))
    assert got == v["expect"], v.get("note")


@pytest.mark.parametrize("v", by_kind("policy"))
def test_policy_validity(v):
    assert outcome(lambda: policy_of(v["policy"])) == v["expect"], v.get("note")


@pytest.mark.parametrize("v", by_kind("id"))
def test_policy_id(v):
    assert policy_of(v["policy"]).spec_id == v["expect"], v.get("note")


@pytest.mark.parametrize("v", by_kind("within"))
def test_within(v):
    got = outcome(lambda: policy_of(v["child"]).within(policy_of(v["parent"])))
    assert got == v["expect"], v.get("note")


@pytest.mark.parametrize("v", by_kind("window"))
def test_window(v):
    w = SpendWindow()
    secs = v["windowSecs"]
    for op in v["ops"]:
        t = op["t"]
        if "record" in op:
            w.record(t, int(op["record"]))
        elif "setWindow" in op:
            w.rewindow(t, secs)  # §6.1: prune under the window that has been in force
            secs = op["setWindow"]
        elif "expect" in op:
            assert w.spent(t, secs) == int(op["expect"]), op.get("note")
        else:
            raise AssertionError(f"unknown op {op}")


class _Tree:
    """Builds the vector's accounts on a fresh Ledger, amply funded, and runs its ops."""

    def __init__(self, v: dict):
        self.L = Ledger()
        self.owner = KeyPair.from_seed(b"o" * 32)
        self.agents: dict[str, Agent] = {}
        for i, a in enumerate(v["accounts"]):
            pol = policy_of(a["policy"])
            if a["parent"] is None:
                ag = Agent(self.L, self.owner, KeyPair.from_seed(bytes([i]) * 32), pol, salt=i)
                self.L.mint(ag.account.address, ASSET, 10 ** 30)
            else:
                parent = self.agents[a["parent"]]
                depth = len(self.L.lineage(parent.account))  # each level keeps most of what it was given
                ag = parent.delegate(KeyPair.from_seed(bytes([i]) * 32), pol, fund=10 ** (24 - 4 * depth), asset=ASSET, salt=i)
            self.agents[a["name"]] = ag

    def run(self, op: dict) -> str:
        self.L.now = op["t"]
        if "spend" in op:
            s = op["spend"]
            assert not s.get("escalated"), "escalated tree spends are not in this vector set"
            return outcome(lambda: self.agents[s["from"]].transfer(s["payee"].lower(), ASSET, int(s["amount"])))
        if "fund" in op:
            f = op["fund"]
            # the reference has no separate fund op after delegation: a transfer whose payee is an
            # in-tree address is the internal move (§4.3), and that rule is what this exercises
            parent, child = self.agents[f["from"]], self.agents[f["to"]]
            body = {"account": parent.account.id, "nonce": parent.account.nonce, "op": "transfer",
                    "to": child.account.address, "asset": ASSET, "amount": int(f["amount"])}
            # straight to the ledger: the agent-side signer would apply its own policy to the
            # amount, and the ledger is the party that knows the payee is inside the tree
            return outcome(lambda: self.L.apply(parent.signer.sign_plain(body)))
        if "recall" in op:
            r = op["recall"]
            return outcome(lambda: self.agents[r["from"]].recall(self.agents[r["of"]], ASSET, int(r["amount"])))
        if "setPolicy" in op:
            s = op["setPolicy"]
            pol = policy_of(s["policy"])
            of = self.agents[s["of"]]
            if s["by"] == "owner":
                body = {"account": of.account.id, "nonce": of.account.nonce, "op": "set_policy", "policy": pol.to_dict()}
                def _self():
                    self.L.apply(sign(self.owner, body))
                    of.signer.set_policy(pol, self.L.now)
                return outcome(_self)
            return outcome(lambda: self.agents[s["by"]].set_child_policy(of, pol))
        if "spentInWindow" in op:
            a = self.agents[op["spentInWindow"]["of"]].account
            return str(a.window.spent(self.L.now, a.policy.window_secs))
        raise AssertionError(f"unknown op {op}")


@pytest.mark.parametrize("v", by_kind("tree"))
def test_tree(v):
    tree = _Tree(v)
    for op in v["ops"]:
        assert tree.run(op) == op["expect"], (op, op.get("note"))


@pytest.mark.parametrize("v", by_kind("x402-exact"))
def test_x402_exact(v):
    chain = [policy_of(p) for p in v["chain"]]
    spent = [int(s) for s in v["spentInWindow"]]
    try:
        spend = check_exact(v["payment"], v["requirements"], chain, spent, v["now"], escalated=v["escalated"],
                            expected_payer=v.get("expectedPayer"))
    except PolicyViolation as e:
        assert e.code == v["expect"], v.get("note")
        return
    assert v["expect"] == "ok", v.get("note")
    exp = v["spend"]
    assert (str(spend.amount), spend.payee, spend.asset, spend.network, spend.payer) == \
        (exp["amount"], exp["payee"], exp["asset"], exp["network"], exp["payer"])
