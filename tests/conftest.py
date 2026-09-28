import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from foliant import Agent, KeyPair, Ledger, Policy

ASSET = "U"


@pytest.fixture
def world():
    L = Ledger()
    payee = KeyPair.from_seed(b"payee")
    L.mint(payee.address, ASSET, 10_000)
    return L, payee


def make_agent(L: Ledger, name: str, policy: Policy | None = None, funds: int = 10_000) -> Agent:
    policy = policy or Policy(per_tx_max=1_000, per_window_max=5_000, window_secs=3_600)
    a = Agent(L, KeyPair.from_seed(name.encode() + b"o"), KeyPair.from_seed(name.encode() + b"s"), policy)
    L.mint(a.account.address, ASSET, funds)
    return a
