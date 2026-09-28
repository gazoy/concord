"""Account, policy, attestation and market-object tests."""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from concord import Attestation, Contribution, KeyPair, Ledger, Policy, ServiceOffer, royalty_split, sign
from concord.accounts import AgentAccount
from concord.errors import InvalidUpdate, PolicyViolation, Unauthorized
from tests.conftest import ASSET, make_agent


@settings(max_examples=200, deadline=None)
@given(
    per_tx=st.integers(1, 100),
    per_window=st.integers(1, 500),
    window=st.integers(1, 100),
    spends=st.lists(st.tuples(st.integers(0, 50), st.integers(1, 120)), max_size=40),  # (gap secs, amount)
)
def test_policy_window_never_exceeded(per_tx, per_window, window, spends):
    """Whatever sequence of transfers is attempted, committed value in any window <= per_window_max."""
    L = Ledger()
    a = make_agent(L, "a", Policy(per_tx_max=per_tx, per_window_max=per_window, window_secs=window), funds=10**6)
    history: list[tuple[int, int]] = []
    for gap, amt in spends:
        L.advance(gap)
        try:
            a.transfer("sink", ASSET, amt)
            history.append((L.now, amt))
            assert amt <= per_tx
        except PolicyViolation:
            pass
        # check the invariant at every step, from both the signer's and the ledger's view
        in_window = sum(x for t, x in history if t > L.now - window)
        assert in_window <= per_window
        assert a.account.window.spent(L.now, window) == in_window
        assert a.signer.window.spent(L.now, window) == in_window


def test_escalation_lets_owner_exceed_per_tx(world):
    L, _ = world
    esc = KeyPair.from_seed(b"esc")
    a = make_agent(L, "a", Policy(per_tx_max=10, per_window_max=1000, window_secs=60, escalation=esc.public))
    with pytest.raises(PolicyViolation):
        a.transfer("sink", ASSET, 50)
    a.transfer("sink", ASSET, 50, escalate_with=esc)
    assert L.balance("sink", ASSET) == 50
    wrong = KeyPair.from_seed(b"wrong")
    with pytest.raises((Unauthorized, PolicyViolation)):
        a.transfer("sink", ASSET, 50, escalate_with=wrong)


def test_nonce_replay_rejected(world):
    L, _ = world
    a = make_agent(L, "a")
    env = a.signer.sign_plain({"account": a.account.id, "nonce": a.account.nonce, "op": "transfer",
                               "to": "sink", "asset": ASSET, "amount": 1})
    L.apply(env)
    with pytest.raises(InvalidUpdate):
        L.apply(env)


def test_owner_rotates_signer_and_old_key_is_dead(world):
    L, _ = world
    a = make_agent(L, "a")
    old = a.signer
    new_kp = KeyPair.from_seed(b"new")
    env = sign(a.owner, {"account": a.account.id, "nonce": a.account.nonce, "op": "rotate_signer",
                         "new_signer": new_kp.public.to_dict()})
    L.apply(env)
    stale = old.sign_plain({"account": a.account.id, "nonce": a.account.nonce, "op": "transfer",
                            "to": "sink", "asset": ASSET, "amount": 1})
    with pytest.raises(Unauthorized):
        L.apply(stale)
    # and the signer cannot rotate itself
    bad = old.sign_plain({"account": a.account.id, "nonce": a.account.nonce, "op": "rotate_signer",
                          "new_signer": old.public.to_dict()})
    with pytest.raises(Unauthorized):
        L.apply(bad)


def test_attestation_requires_trusted_vendor(world):
    from concord import Agent
    L, _ = world
    vendor, rogue = KeyPair.from_seed(b"vendor"), KeyPair.from_seed(b"rogue")
    L.trusted_vendors.add(vendor.public.hex)
    signer = KeyPair.from_seed(b"s")
    pol = Policy(1, 1, 1)
    good = Attestation.issue(vendor, "codehash-1", signer.public)
    bad = Attestation.issue(rogue, "codehash-1", signer.public)
    Agent(L, KeyPair.from_seed(b"o1"), signer, pol, attestation=good, salt=1)
    with pytest.raises(Unauthorized):
        Agent(L, KeyPair.from_seed(b"o2"), signer, pol, attestation=bad, salt=2)
    # quote for a different key is rejected
    other = KeyPair.from_seed(b"other")
    mismatched = Attestation.issue(vendor, "codehash-1", other.public)
    with pytest.raises(Unauthorized):
        Agent(L, KeyPair.from_seed(b"o3"), signer, pol, attestation=mismatched, salt=3)


def test_royalty_cascade_conserves_revenue():
    g = {}
    root = Contribution("root", "alice", "CC-BY", royalty_bps=1000)
    mid = Contribution("mid", "bob", "CC-BY", royalty_bps=2000, parent="root")
    leaf = Contribution("leaf", "carol", "CC-BY", royalty_bps=0, parent="mid")
    for c in (root, mid, leaf):
        g[c.id] = c
    split = royalty_split(g, "leaf", 10_000)
    assert sum(split.values()) == 10_000
    assert split["bob"] == 2_000            # 20% of 10,000
    assert split["alice"] == 800            # 10% of the remaining 8,000
    assert split["carol"] == 7_200


def test_offer_id_is_content_addressed():
    o1 = ServiceOffer(provider="p", asset=ASSET, price_per_unit=3, unit="call")
    o2 = ServiceOffer(provider="p", asset=ASSET, price_per_unit=3, unit="call")
    o3 = ServiceOffer(provider="p", asset=ASSET, price_per_unit=4, unit="call")
    assert o1.id == o2.id != o3.id
