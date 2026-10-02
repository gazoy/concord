import contextlib
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from foliant import Agent, KeyPair, Ledger, Policy

ASSET = "U"

ANVIL_ATTEMPTS = 3
ANVIL_WAIT_SECS = 20


def free_port() -> int:
    """A port nothing is listening on *right now*. See `anvil` for why that is not a guarantee."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def anvil():
    """Start anvil on a free port, wait for it to answer, and kill it afterwards.

    Two things this replaces. `free_port` binds a port, reads the number and closes the socket, so
    the port can be taken by something else before anvil binds it; nothing here can close that
    race, because anvil has to do its own binding. So a failure to come up is retried on a fresh
    port rather than treated as impossible. And the wait now raises when it runs out: the loop this
    replaces tried for ten seconds and then fell through in silence, so the first thing to touch
    the dead node failed with an unrelated connection error and the actual cause never appeared.

    web3 is imported here rather than at module scope: it is an optional extra (`chain`), and
    conftest is imported for every test run including those that do not need it.
    """
    from web3 import Web3

    last = "no attempt made"
    for _ in range(ANVIL_ATTEMPTS):
        port = free_port()
        proc = subprocess.Popen(["anvil", "--port", str(port), "--silent"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}"))
        up, deadline = False, time.monotonic() + ANVIL_WAIT_SECS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last = f"anvil exited with code {proc.returncode}; port {port} was probably taken"
                break
            try:
                w3.eth.chain_id
                up = True
                break
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(0.1)
        else:
            last = f"anvil did not answer on port {port} within {ANVIL_WAIT_SECS}s ({last})"
        if up:
            try:
                yield w3, port
            finally:
                proc.kill()
            return
        proc.kill()
    raise RuntimeError(f"anvil did not start in {ANVIL_ATTEMPTS} attempts; last failure: {last}")


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


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _gate_state_dir(tmp_path, monkeypatch):
    """Each test gets its own directory for ChainGate's persisted state (foliant.chain)."""
    monkeypatch.setenv("FOLIANT_STATE_DIR", str(tmp_path))
