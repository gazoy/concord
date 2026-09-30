"""demo/tap.py against Anvil (TAP-REVIEW.md): limits charged before broadcast and persisted, contract
recipients refused, failures after broadcast leave the address spent, two instances over one state
directory agree, and the HTTP layer behaves (404 without a key, 400 on bad input, non-blocking)."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time

import pytest
from eth_account import Account
from web3 import Web3

from foliant.chain import ChainLedger, deploy_local

pytestmark = pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil not installed")

DEPLOYER = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
PROVIDER = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
TAP_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def chain():
    port = _free_port()
    proc = subprocess.Popen(["anvil", "--port", str(port), "--silent"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}"))
    for _ in range(100):
        try:
            w3.eth.chain_id
            break
        except Exception:
            time.sleep(0.1)
    addrs = deploy_local(w3, DEPLOYER)
    L = ChainLedger("", addrs["accounts"], addrs["channels"], addrs["pools"], w3=w3)
    yield L, addrs, port
    proc.kill()


def _tap(chain, tmp_path, **kw):
    from demo.tap import Tap
    L, addrs, _ = chain
    args = dict(avax_wei=Web3.to_wei("0.02", "ether"), tokens=1000 * 10 ** 6, per_hour=3, state_dir=str(tmp_path))
    args.update(kw)
    return Tap(L, TAP_KEY, addrs["token"], **args)


def test_tap_funds_once_per_address(chain, tmp_path):
    from demo.tap import TapError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path)
    visitor = Account.create().address
    out = tap.give(visitor.lower())  # lowercase skips the checksum
    assert len(out["transactions"]) == 2 and all(t.startswith("0x") for t in out["transactions"])
    assert L.w3.eth.get_balance(visitor) == Web3.to_wei("0.02", "ether")
    assert L.token(addrs["token"]).functions.balanceOf(visitor).call() == 1000 * 10 ** 6
    with pytest.raises(TapError) as e:
        tap.give(visitor)
    assert e.value.status == 429
    again = _tap(chain, tmp_path)  # persisted: a fresh instance over the same directory still refuses
    with pytest.raises(TapError):
        again.give(visitor)
    assert again.status()["given"] == 1


def test_tap_rejects_bad_input(chain, tmp_path):
    from demo.tap import TapError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path)
    good = Account.create().address  # checksummed
    wrong_checksum = good[:2] + good[2:].swapcase()
    for bad in (None, "", "0x1234", 42, wrong_checksum, addrs["accounts"]):  # the last is a contract
        with pytest.raises(TapError) as e:
            tap.give(bad)
        assert e.value.status == 400, bad
    assert tap.status()["given"] == 0  # nothing charged for a refused request
    assert L.w3.eth.get_balance(good) == 0


def test_tap_hourly_cap_persists(chain, tmp_path):
    from demo.tap import TapError
    tap = _tap(chain, tmp_path, per_hour=2)
    tap.give(Account.create().address)
    tap.give(Account.create().address)
    with pytest.raises(TapError) as e:
        tap.give(Account.create().address)
    assert e.value.status == 429 and "busy" in str(e.value)
    restarted = _tap(chain, tmp_path, per_hour=2)  # the cap survives a restart
    with pytest.raises(TapError) as e:
        restarted.give(Account.create().address)
    assert "busy" in str(e.value)


def test_tap_dry_wallet_charges_nothing(chain, tmp_path):
    from demo.tap import TapError
    L, _, _ = chain
    tap = _tap(chain, tmp_path, avax_wei=L.w3.eth.get_balance(Account.from_key(TAP_KEY).address) + 1)
    visitor = Account.create().address
    with pytest.raises(TapError) as e:
        tap.give(visitor)
    assert e.value.status == 503
    assert L.w3.eth.get_balance(visitor) == 0
    assert tap.status()["given"] == 0  # a pre-check failure is not an attempt


def test_failure_after_broadcast_spends_the_address(chain, tmp_path, monkeypatch):
    """The mint succeeds, the AVAX transfer fails to confirm: the address is marked, the mint stands, and
    a retry is refused rather than funding again (T-2/T-3)."""
    from demo.tap import TapError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path)
    visitor = Account.create().address
    real_send = tap._send
    calls = {"n": 0}

    def flaky(tx):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TapError(504, "simulated: not confirmed")
        return real_send(tx)

    monkeypatch.setattr(tap, "_send", flaky)
    with pytest.raises(TapError) as e:
        tap.give(visitor)
    assert e.value.status == 504
    assert L.token(addrs["token"]).functions.balanceOf(visitor).call() == 1000 * 10 ** 6  # the mint went first
    assert L.w3.eth.get_balance(visitor) == 0
    monkeypatch.setattr(tap, "_send", real_send)
    with pytest.raises(TapError) as e:
        tap.give(visitor)
    assert e.value.status == 429  # spent, not eligible again
    saved = json.loads(next(p for p in tmp_path.iterdir() if p.suffix == ".json").read_text())
    assert saved["given"][visitor.lower()].startswith("failed:")


def test_two_instances_one_directory(chain, tmp_path):
    """Two processes' worth of taps over one state directory: the same address is funded exactly once,
    and the hourly count is shared (T-5/T-6)."""
    from demo.tap import TapError
    L, _, _ = chain
    a, b = _tap(chain, tmp_path, per_hour=5), _tap(chain, tmp_path, per_hour=5)
    visitor = Account.create().address
    results = []

    def go(t):
        try:
            t.give(visitor)
            results.append("ok")
        except TapError as e:
            results.append(e.status)

    ts = [threading.Thread(target=go, args=(t,)) for t in (a, b)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert sorted(map(str, results)) == ["503", "ok"]  # the loser is told at once, not queued (R2-3)
    assert L.w3.eth.get_balance(visitor) == Web3.to_wei("0.02", "ether")
    for t in (a, b):  # afterwards both instances know the address is spent
        with pytest.raises(TapError) as e:
            t.give(visitor)
        assert e.value.status == 429


def test_failure_before_broadcast_releases_the_address(chain, tmp_path, monkeypatch):
    """An RPC error before anything reaches the chain: 502, nothing charged, the address may try again (R2-2)."""
    from demo.tap import TapError
    L, _, _ = chain
    tap = _tap(chain, tmp_path, per_hour=2)
    visitor = Account.create().address

    def rpc_down(*a, **k):
        raise ConnectionError("simulated RPC outage")

    monkeypatch.setattr(L.w3.eth, "get_transaction_count", rpc_down)
    with pytest.raises(TapError) as e:
        tap.give(visitor)
    assert e.value.status == 502
    monkeypatch.undo()
    assert tap.status()["given"] == 0
    assert tap.give(visitor)["transactions"]  # eligible: nothing had been broadcast


def test_in_flight_transaction_refuses_new_attempts(chain, tmp_path):
    """A stuck transaction from the tap key must not make the next visitor's attempt fail (R2-1)."""
    from demo.tap import TapError
    L, _, _ = chain
    tap = _tap(chain, tmp_path)
    w3 = L.w3
    w3.provider.make_request("evm_setAutomine", [False])
    try:
        k = Account.from_key(TAP_KEY)
        tx = {"to": k.address, "value": 0, "gas": 21_000, "gasPrice": w3.eth.gas_price,
              "nonce": w3.eth.get_transaction_count(k.address, "latest"), "chainId": L.chain_id}
        w3.eth.send_raw_transaction(k.sign_transaction(tx).raw_transaction)
        visitor = Account.create().address
        with pytest.raises(TapError) as e:
            tap.give(visitor)
        assert e.value.status == 503 and "in flight" in str(e.value)
    finally:
        w3.provider.make_request("evm_setAutomine", [True])
        w3.provider.make_request("evm_mine", [])
    assert tap.status()["given"] == 0
    assert tap.give(visitor)["transactions"]  # clears once mined


def test_uppercase_hex_accepted(chain, tmp_path):
    tap = _tap(chain, tmp_path)
    v = Account.create().address
    assert tap.give("0x" + v[2:].upper())["address"] == v


_salt = [1000]


def _app(chain, tmp_path, with_tap=True):
    L, addrs, port = chain
    _salt[0] += 1
    env = {"FOLIANT_RPC": f"http://127.0.0.1:{port}", "FOLIANT_ACCOUNTS": addrs["accounts"], "FOLIANT_CHANNELS": addrs["channels"],
           "FOLIANT_POOLS": addrs["pools"], "FOLIANT_TOKEN": addrs["token"], "PROVIDER_KEY": PROVIDER,
           "FOLIANT_STATE_DIR": str(tmp_path), "FOLIANT_POOL_SALT": str(_salt[0])}
    if with_tap:
        env["FOLIANT_TAP_KEY"] = TAP_KEY
    old = dict(os.environ)
    os.environ.update(env)
    try:
        from demo.serve_chain import make_app
        return make_app()
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_http_layer(chain, tmp_path):
    from fastapi.testclient import TestClient
    L, _, _ = chain
    c = TestClient(_app(chain, tmp_path))
    assert c.get("/chain").json()["tap"]["perHour"] == 20
    assert c.post("/tap", content=b"nope").status_code == 400
    assert c.post("/tap", json={"address": "0x12"}).status_code == 400
    v = Account.create().address
    r = c.post("/tap", json={"address": v})
    assert r.status_code == 200 and len(r.json()["transactions"]) == 2
    assert c.post("/tap", json={"address": v}).status_code == 429
    assert L.w3.eth.get_balance(v) == Web3.to_wei("0.02", "ether")
    c2 = TestClient(_app(chain, tmp_path / "notap", with_tap=False))
    assert c2.post("/tap", json={"address": v}).status_code == 404
    assert c2.get("/chain").json()["tap"] is None


def test_shared_key_refused(chain, tmp_path, monkeypatch):
    L, addrs, port = chain
    monkeypatch.setenv("FOLIANT_TAP_KEY", PROVIDER)
    for k, v in {"FOLIANT_RPC": f"http://127.0.0.1:{port}", "FOLIANT_ACCOUNTS": addrs["accounts"], "FOLIANT_CHANNELS": addrs["channels"],
                 "FOLIANT_POOLS": addrs["pools"], "FOLIANT_TOKEN": addrs["token"], "PROVIDER_KEY": PROVIDER,
                 "FOLIANT_STATE_DIR": str(tmp_path), "FOLIANT_POOL_SALT": "777"}.items():
        monkeypatch.setenv(k, v)
    from demo.serve_chain import make_app
    with pytest.raises(SystemExit):
        make_app()


def test_balance_cache_invalidated_by_own_spending(chain, tmp_path):
    """Two gives inside the 30 s cache window when the wallet can afford only one: the second is refused
    dry, nothing charged, rather than broadcasting a mint and then failing the transfer (R3-1)."""
    from demo.tap import TapError
    L, addrs, _ = chain
    bal = L.w3.eth.get_balance(Account.from_key(TAP_KEY).address)
    tap = _tap(chain, tmp_path, avax_wei=int(bal * 0.6))
    first = Account.create().address
    assert tap.give(first)["transactions"]
    second = Account.create().address
    with pytest.raises(TapError) as e:
        tap.give(second)
    assert e.value.status == 503 and "dry" in str(e.value)
    assert L.token(addrs["token"]).functions.balanceOf(second).call() == 0
    assert tap.status()["given"] == 1
    # refill the tap so later tests have gas
    d = Account.from_key(DEPLOYER)
    tx = {"to": Account.from_key(TAP_KEY).address, "value": int(bal * 0.6), "gas": 21_000, "gasPrice": L.w3.eth.gas_price,
          "nonce": L.w3.eth.get_transaction_count(d.address), "chainId": L.chain_id}
    L.w3.eth.wait_for_transaction_receipt(L.w3.eth.send_raw_transaction(d.sign_transaction(tx).raw_transaction))
