"""Hierarchical budgets (whitepaper §6.2): no branch ever commits more than any ancestor allows."""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from foliant import Agent, KeyPair, Policy
from foliant.crypto import sign
from foliant.errors import PolicyViolation, Unauthorized

from tests.conftest import ASSET, make_agent

HOUR = 3600
OUTSIDE = KeyPair.from_seed(b"outside").address


def root_agent(L, per_window=1_000, per_tx=200, **kw) -> Agent:
    return make_agent(L, "root", Policy(per_tx_max=per_tx, per_window_max=per_window, window_secs=HOUR, **kw))


def test_child_policy_must_sit_within_parent(world):
    L, _ = world
    root = root_agent(L, allow_list=frozenset({OUTSIDE}), expiry=L.now + HOUR)
    kp = KeyPair.from_seed(b"c1")
    for bad in (
        dict(per_tx_max=201, per_window_max=100, window_secs=HOUR, allow_list=frozenset({OUTSIDE}), expiry=L.now + 10),
        dict(per_tx_max=10, per_window_max=1_001, window_secs=HOUR, allow_list=frozenset({OUTSIDE}), expiry=L.now + 10),
        dict(per_tx_max=10, per_window_max=100, window_secs=HOUR, allow_list=None, expiry=L.now + 10),
        dict(per_tx_max=10, per_window_max=100, window_secs=HOUR, allow_list=frozenset({OUTSIDE, "x"}), expiry=L.now + 10),
        dict(per_tx_max=10, per_window_max=100, window_secs=HOUR, allow_list=frozenset({OUTSIDE}), expiry=None),
        dict(per_tx_max=10, per_window_max=100, window_secs=HOUR, allow_list=frozenset({OUTSIDE}), expiry=L.now + 2 * HOUR),
    ):
        with pytest.raises(PolicyViolation):
            root.delegate(kp, Policy(**bad), fund=10, asset=ASSET)
    child = root.delegate(kp, Policy(per_tx_max=10, per_window_max=100, window_secs=HOUR,
                                     allow_list=frozenset({OUTSIDE}), expiry=L.now + 10), fund=10, asset=ASSET)
    assert child.account.parent == root.account.id


def test_funding_and_recall_are_not_spends(world):
    L, _ = world
    root = root_agent(L, per_window=100)
    child = root.delegate(KeyPair.from_seed(b"c"), Policy(per_tx_max=50, per_window_max=100, window_secs=HOUR),
                          fund=5_000, asset=ASSET)  # far more than the window: allowed, it stays in the tree
    assert L.balance(child.account.address, ASSET) == 5_000
    assert root.account.window.spent(L.now, HOUR) == 0
    grandchild = child.delegate(KeyPair.from_seed(b"g"), Policy(per_tx_max=50, per_window_max=100, window_secs=HOUR),
                                fund=1_000, asset=ASSET)
    assert root.recall(grandchild, ASSET)["recalled"] == 1_000  # any descendant, not just a direct child
    assert L.balance(root.account.address, ASSET) == 10_000 - 5_000 + 1_000
    assert root.account.window.spent(L.now, HOUR) == 0


def test_spend_counts_against_every_ancestor(world):
    L, _ = world
    root = root_agent(L, per_window=100, per_tx=100)
    a = root.delegate(KeyPair.from_seed(b"a"), Policy(per_tx_max=100, per_window_max=100, window_secs=HOUR), fund=500, asset=ASSET)
    b = root.delegate(KeyPair.from_seed(b"b"), Policy(per_tx_max=100, per_window_max=100, window_secs=HOUR), fund=500, asset=ASSET, salt=1)
    a.transfer(OUTSIDE, ASSET, 60)
    assert root.account.window.spent(L.now, HOUR) == 60
    with pytest.raises(PolicyViolation, match="per_window_max 100"):
        b.transfer(OUTSIDE, ASSET, 50)  # b's own policy allows it; the root's does not
    assert b.account.window.spent(L.now, HOUR) == 0  # all-or-nothing: nothing recorded on failure
    assert L.balance(b.account.address, ASSET) == 500
    b.transfer(OUTSIDE, ASSET, 40)
    assert root.account.window.spent(L.now, HOUR) == 100


def test_tightened_parent_binds_existing_children(world):
    L, _ = world
    root = root_agent(L, per_window=1_000)
    child = root.delegate(KeyPair.from_seed(b"c"), Policy(per_tx_max=200, per_window_max=500, window_secs=HOUR), fund=500, asset=ASSET)
    owner_env = sign(root.owner, {"account": root.account.id, "nonce": root.account.nonce, "op": "set_policy",
                                  "policy": Policy(per_tx_max=200, per_window_max=30, window_secs=HOUR).to_dict()})
    L.apply(owner_env)
    with pytest.raises(PolicyViolation):
        child.transfer(OUTSIDE, ASSET, 31)  # child's policy still says 500; the root now says 30
    child.transfer(OUTSIDE, ASSET, 30)


def test_ancestor_signer_administers_descendants(world):
    L, _ = world
    root = root_agent(L)
    child = root.delegate(KeyPair.from_seed(b"c"), Policy(per_tx_max=200, per_window_max=500, window_secs=HOUR), fund=500, asset=ASSET)
    root.set_child_policy(child, Policy(per_tx_max=0, per_window_max=0, window_secs=HOUR, expiry=L.now))
    with pytest.raises(PolicyViolation):
        child.transfer(OUTSIDE, ASSET, 1)
    with pytest.raises(Unauthorized):  # a child cannot set its own policy at all
        L.apply(child.signer.sign_plain({"account": child.account.id, "nonce": child.account.nonce, "op": "set_policy",
                                         "policy": Policy(per_tx_max=200, per_window_max=500, window_secs=HOUR).to_dict()}))
    stranger = make_agent(L, "stranger")
    with pytest.raises(Unauthorized):
        stranger.recall(child, ASSET)
    with pytest.raises(Unauthorized):
        L.apply(stranger.signer.sign_plain({"account": child.account.id, "nonce": child.account.nonce, "op": "rotate_signer",
                                            "new_signer": stranger.signer.public.to_dict()}))


@settings(max_examples=60, deadline=None)
@given(
    root_cap=st.integers(1, 300),
    caps=st.lists(st.integers(1, 400), min_size=1, max_size=4),
    spends=st.lists(st.tuples(st.integers(0, 6), st.integers(1, 120)), min_size=1, max_size=40),
)
def test_no_branch_exceeds_any_ancestor(root_cap, caps, spends):
    """Random two-level tree, random spends from any node; every window stays within
    its own cap and the sum over any subtree stays within that subtree root's cap."""
    from foliant import Ledger
    L = Ledger()
    root = make_agent(L, "r", Policy(per_tx_max=1_000, per_window_max=root_cap, window_secs=HOUR), funds=100_000)
    nodes = [root]
    for i, c in enumerate(caps):
        cap = min(c, root_cap)
        child = root.delegate(KeyPair.from_seed(f"c{i}".encode()), Policy(per_tx_max=1_000, per_window_max=cap, window_secs=HOUR),
                              fund=10_000, asset=ASSET, salt=i)
        nodes.append(child)
        if i % 2 == 0:
            nodes.append(child.delegate(KeyPair.from_seed(f"g{i}".encode()),
                                        Policy(per_tx_max=1_000, per_window_max=cap, window_secs=HOUR), fund=2_000, asset=ASSET))
    committed = {n.account.id: 0 for n in nodes}
    for idx, amt in spends:
        n = nodes[idx % len(nodes)]
        try:
            n.transfer(OUTSIDE, ASSET, amt)
            committed[n.account.id] += amt
        except PolicyViolation:
            pass
    for n in nodes:
        subtree = sum(v for k, v in committed.items() if k == n.account.id or L.is_descendant(k, n.account.id))
        assert subtree <= n.account.policy.per_window_max
        assert n.account.window.spent(L.now, HOUR) == subtree
    assert L.total_supply(ASSET) == 100_000
