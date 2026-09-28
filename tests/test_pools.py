"""Pool invariants.

P1  Coordinator receives exactly the sum of members' highest signed balances, never more.
P2  Any member can exit unilaterally and receives deposit minus its highest signed balance,
    whether or not the coordinator ever settles.
P3  A member exiting with a stale update is corrected by the coordinator's contest.
P4  Conservation across all members, the coordinator and escrow.
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from concord import KeyPair, Ledger
from concord.errors import InvalidUpdate
from tests.conftest import ASSET, make_agent


@settings(max_examples=100, deadline=None)
@given(
    n=st.integers(1, 4),
    rounds=st.lists(st.lists(st.integers(0, 20), min_size=4, max_size=4), min_size=0, max_size=15),
    settle_after=st.lists(st.booleans(), min_size=15, max_size=15),
    coordinator_alive=st.booleans(),
)
def test_pool_conservation_and_exit(n, rounds, settle_after, coordinator_alive):
    L = Ledger()
    coord = KeyPair.from_seed(b"c")
    L.mint(coord.address, ASSET, 100)
    pool = L.create_pool(coord.address, ASSET, timeout_secs=10, bond=100)
    agents = [make_agent(L, f"m{i}") for i in range(n)]
    deposit = 200
    for a in agents:
        a.join_pool(pool.id, deposit)
    signed = [0] * n
    for r, amounts in enumerate(rounds):
        for i, a in enumerate(agents[:n]):
            amt = amounts[i]
            if amt == 0 or signed[i] + amt > deposit:
                continue
            a.pay_pool(pool.id, amt)
            signed[i] += amt
        if settle_after[r] and coordinator_alive:
            L.coordinator_settle_pool(pool.id, [a.latest[pool.id] for a in agents if pool.id in a.latest])
    # P1 so far
    assert L.balance(coord.address, ASSET) <= sum(signed)
    # every member exits unilaterally
    for a in agents:
        a.begin_exit(pool.id)
    L.advance(11)
    for a in agents:
        a.finalize_exit(pool.id)
    # P1, P2, P4
    assert L.balance(coord.address, ASSET) == sum(signed)
    for i, a in enumerate(agents):
        assert L.balance(a.account.address, ASSET) == 10_000 - signed[i]
    assert L.balance(pool.escrow, ASSET) == 0
    assert L.total_supply(ASSET) == 100 + 10_000 * n


def test_stale_exit_is_contested(world):
    L, _ = world
    coord = KeyPair.from_seed(b"c")
    L.mint(coord.address, ASSET, 100)
    pool = L.create_pool(coord.address, ASSET, timeout_secs=10, bond=100)
    a = make_agent(L, "a")
    a.join_pool(pool.id, 100)
    old = a.pay_pool(pool.id, 10)
    new = a.pay_pool(pool.id, 30)  # signed 40 in total
    a.latest[pool.id] = old  # dishonest: exit claiming only 10 paid
    a.begin_exit(pool.id)
    assert L.coordinator_contest_exit(pool.id, new) == 30
    L.advance(10)
    with pytest.raises(InvalidUpdate):
        L.coordinator_contest_exit(pool.id, new)  # window closed
    a.finalize_exit(pool.id)
    assert L.balance(coord.address, ASSET) == 40
    assert L.balance(a.account.address, ASSET) == 10_000 - 40


def test_stale_update_in_batch_is_skipped_not_fatal(world):
    L, _ = world
    coord = KeyPair.from_seed(b"c")
    pool = L.create_pool(coord.address, ASSET, timeout_secs=10, bond=0)
    a, b = make_agent(L, "a"), make_agent(L, "b")
    a.join_pool(pool.id, 100)
    b.join_pool(pool.id, 100)
    ua1 = a.pay_pool(pool.id, 5)
    ua2 = a.pay_pool(pool.id, 5)
    ub1 = b.pay_pool(pool.id, 7)
    assert L.coordinator_settle_pool(pool.id, [ua2]) == 10
    assert L.coordinator_settle_pool(pool.id, [ua1, ub1]) == 7  # ua1 stale, skipped
