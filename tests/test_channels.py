"""Channel invariants.

I1  The payee can never receive more than the highest balance the payer signed.
I2  The payer can never get back more than deposit minus what it signed away.
I3  Conservation: escrow + payee delta + payer refund == deposit, always.
I4  A stale or forged update is rejected.
I5  Unilateral close always completes after the timeout, whatever the payee does.
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from foliant import KeyPair, sign
from foliant.errors import FoliantError, InvalidUpdate, PolicyViolation, Unauthorized
from tests.conftest import ASSET, make_agent


@settings(max_examples=200, deadline=None)
@given(
    deposit=st.integers(1, 500),
    payments=st.lists(st.integers(1, 50), min_size=0, max_size=30),
    settle_at=st.lists(st.booleans(), min_size=30, max_size=30),
    payee_contests=st.booleans(),
)
def test_channel_conservation_and_bounds(deposit, payments, settle_at, payee_contests):
    from foliant import Ledger
    L = Ledger()
    payee = KeyPair.from_seed(b"p")
    a = make_agent(L, "a")
    cid = a.open_channel(payee.address, ASSET, deposit, timeout_secs=10)
    signed_total = 0
    for i, amt in enumerate(payments):
        if signed_total + amt > deposit:
            with pytest.raises(ValueError):
                a.pay_channel(cid, amt)
            continue
        u = a.pay_channel(cid, amt)
        signed_total += amt
        if settle_at[i]:
            L.payee_settle_channel(cid, u)  # payee settles incrementally
    # I1 after settlements so far
    assert L.balance(payee.address, ASSET) <= signed_total
    # payer closes with its latest; payee may contest with the same latest (no higher exists)
    a.close_channel(cid)
    if payee_contests and cid in a.latest:
        with pytest.raises(FoliantError):
            L.payee_contest_close(cid, a.latest[cid])  # not newer than what close applied
    L.advance(11)
    a.finalize_close(cid)
    ch = L.channels[cid]
    # I1, I2, I3
    assert L.balance(payee.address, ASSET) == signed_total
    assert L.balance(a.account.address, ASSET) == 10_000 - signed_total
    assert L.balance(ch.escrow, ASSET) == 0
    assert L.total_supply(ASSET) == 10_000


def test_stale_and_forged_updates_rejected(world):
    L, payee = world
    a = make_agent(L, "a")
    cid = a.open_channel(payee.address, ASSET, 100, timeout_secs=10)
    u1 = a.pay_channel(cid, 10)
    u2 = a.pay_channel(cid, 10)
    assert L.payee_settle_channel(cid, u2) == 20
    with pytest.raises(InvalidUpdate):
        L.payee_settle_channel(cid, u1)  # stale
    # forged: payee signs its own "update"
    forged = sign(payee, {**u2.body, "seq": 99, "balance": 100})
    with pytest.raises(Unauthorized):
        L.payee_settle_channel(cid, forged)
    # tampered: body edited after signing
    from foliant.crypto import Signed
    tampered = Signed({**u2.body, "seq": 3, "balance": 100}, u2.signer, u2.signature)
    with pytest.raises(FoliantError):
        L.payee_settle_channel(cid, tampered)


def test_close_cannot_be_finalised_early(world):
    L, payee = world
    a = make_agent(L, "a")
    cid = a.open_channel(payee.address, ASSET, 100, timeout_secs=10)
    a.pay_channel(cid, 5)
    a.close_channel(cid)
    with pytest.raises(InvalidUpdate):
        a.finalize_close(cid)
    L.advance(10)
    a.finalize_close(cid)
    assert L.balance(a.account.address, ASSET) == 10_000 - 5


def test_payee_wins_close_with_higher_update(world):
    """Payer tries to close with an old update; payee contests with the newer one."""
    L, payee = world
    a = make_agent(L, "a")
    cid = a.open_channel(payee.address, ASSET, 100, timeout_secs=10)
    old = a.pay_channel(cid, 10)
    new = a.pay_channel(cid, 10)
    a.latest[cid] = old  # a dishonest payer presents the old state
    a.close_channel(cid)
    assert L.payee_contest_close(cid, new) == 10  # the extra 10 over `old`
    L.advance(10)
    a.finalize_close(cid)
    assert L.balance(payee.address, ASSET) == 10_000 + 20


def test_stream_accrues_and_caps_at_deposit(world):
    L, payee = world
    a = make_agent(L, "a")
    cid = a.open_channel(payee.address, ASSET, 100, timeout_secs=10)
    a.start_stream(cid, rate_per_sec=7)
    L.advance(5)
    assert L.payee_claim_stream(cid) == 35
    L.advance(100)
    assert L.payee_claim_stream(cid) == 65  # capped at deposit
    L.stop_stream(cid)
    assert L.payee_claim_stream(cid) == 0
    assert L.total_supply(ASSET) == 20_000


def test_policy_allow_list_blocks_off_chain_signing(world):
    from foliant import Policy
    L, payee = world
    other = KeyPair.from_seed(b"other")
    pol = Policy(per_tx_max=1000, per_window_max=5000, window_secs=60, allow_list=frozenset({payee.address}))
    a = make_agent(L, "a", pol)
    with pytest.raises(PolicyViolation):
        a.open_channel(other.address, ASSET, 10)
    cid = a.open_channel(payee.address, ASSET, 10)
    a.pay_channel(cid, 1)
