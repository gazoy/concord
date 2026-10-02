"""demo/tap.py against Anvil (TAP-REVIEW.md): limits charged before broadcast and persisted, contract
recipients refused, failures after broadcast leave the address spent, two instances over one state
directory agree, and the HTTP layer behaves (404 without a key, 400 on bad input, non-blocking); then the
TAP-AUDIT-FINAL fixes: per-client and daily caps (F-1), a send whose retry errors while the transaction is
in the pool counts as broadcast (F-2), stuck transactions are recorded and named (F-3), receipts are polled
every 2 s (F-4); and the "can wait" set: give() never trusts status()'s balance cache (F-5), the state file
is validated and never takes the server down (F-6), /chain survives a balance RPC failure (F-7), recipients
that cannot receive a plain transfer are refused before the charge (F-8), the file locks do not leak a
descriptor (F-9), addresses must carry their 0x prefix (F-10), and bodies over 1 KiB are refused (F-11)."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time

import pytest
from eth_account import Account
from web3 import Web3

from foliant.chain import ChainLedger, deploy_local

from tests.conftest import anvil

pytestmark = pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil not installed")

DEPLOYER = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
PROVIDER = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
TAP_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"


@pytest.fixture(scope="module")
def chain():
    with anvil() as (w3, port):
        addrs = deploy_local(w3, DEPLOYER)
        L = ChainLedger("", addrs["accounts"], addrs["channels"], addrs["pools"], w3=w3)
        yield L, addrs, port


def _tap(chain, tmp_path, **kw):
    from demo.tap import Tap
    L, addrs, _ = chain
    # the per-client cap is generous here so the older tests, which all use client "test", are not hit by it
    args = dict(avax_wei=Web3.to_wei("0.02", "ether"), tokens=1000 * 10 ** 6, per_hour=3, per_client_day=100,
                state_dir=str(tmp_path))
    args.update(kw)
    return Tap(L, TAP_KEY, addrs["token"], **args)


def test_tap_funds_once_per_address(chain, tmp_path):
    from demo.tap import TapError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path)
    visitor = Account.create().address
    out = tap.give(visitor.lower(), "test")  # lowercase skips the checksum
    assert len(out["transactions"]) == 2 and all(t.startswith("0x") for t in out["transactions"])
    assert L.w3.eth.get_balance(visitor) == Web3.to_wei("0.02", "ether")
    assert L.token(addrs["token"]).functions.balanceOf(visitor).call() == 1000 * 10 ** 6
    with pytest.raises(TapError) as e:
        tap.give(visitor, "test")
    assert e.value.status == 429
    again = _tap(chain, tmp_path)  # persisted: a fresh instance over the same directory still refuses
    with pytest.raises(TapError):
        again.give(visitor, "test")
    assert again.status()["given"] == 1


def test_tap_rejects_bad_input(chain, tmp_path):
    from demo.tap import TapError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path)
    good = Account.create().address  # checksummed
    wrong_checksum = good[:2] + good[2:].swapcase()
    for bad in (None, "", "0x1234", 42, wrong_checksum, addrs["accounts"]):  # the last is a contract
        with pytest.raises(TapError) as e:
            tap.give(bad, "test")
        assert e.value.status == 400, bad
    assert tap.status()["given"] == 0  # nothing charged for a refused request
    assert L.w3.eth.get_balance(good) == 0


def test_tap_hourly_cap_persists(chain, tmp_path):
    from demo.tap import TapError
    tap = _tap(chain, tmp_path, per_hour=2)
    tap.give(Account.create().address, "test")
    tap.give(Account.create().address, "test")
    with pytest.raises(TapError) as e:
        tap.give(Account.create().address, "test")
    assert e.value.status == 429 and "busy" in str(e.value)
    restarted = _tap(chain, tmp_path, per_hour=2)  # the cap survives a restart
    with pytest.raises(TapError) as e:
        restarted.give(Account.create().address, "test")
    assert "busy" in str(e.value)


def test_tap_dry_wallet_charges_nothing(chain, tmp_path):
    from demo.tap import TapError
    L, _, _ = chain
    tap = _tap(chain, tmp_path, avax_wei=L.w3.eth.get_balance(Account.from_key(TAP_KEY).address) + 1)
    visitor = Account.create().address
    with pytest.raises(TapError) as e:
        tap.give(visitor, "test")
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
        tap.give(visitor, "test")
    assert e.value.status == 504
    assert L.token(addrs["token"]).functions.balanceOf(visitor).call() == 1000 * 10 ** 6  # the mint went first
    assert L.w3.eth.get_balance(visitor) == 0
    monkeypatch.setattr(tap, "_send", real_send)
    with pytest.raises(TapError) as e:
        tap.give(visitor, "test")
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
            t.give(visitor, "test")
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
            t.give(visitor, "test")
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
        tap.give(visitor, "test")
    assert e.value.status == 502
    monkeypatch.undo()
    assert tap.status()["given"] == 0
    assert tap.give(visitor, "test")["transactions"]  # eligible: nothing had been broadcast


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
            tap.give(visitor, "test")
        assert e.value.status == 503 and "in flight" in str(e.value)
    finally:
        w3.provider.make_request("evm_setAutomine", [True])
        w3.provider.make_request("evm_mine", [])
    assert tap.status()["given"] == 0
    assert tap.give(visitor, "test")["transactions"]  # clears once mined


def test_uppercase_hex_accepted(chain, tmp_path):
    tap = _tap(chain, tmp_path)
    v = Account.create().address
    assert tap.give("0x" + v[2:].upper(), "test")["address"] == v


_salt = [1000]


def _app(chain, tmp_path, with_tap=True, **extra):
    L, addrs, port = chain
    _salt[0] += 1
    env = {"FOLIANT_RPC": f"http://127.0.0.1:{port}", "FOLIANT_ACCOUNTS": addrs["accounts"], "FOLIANT_CHANNELS": addrs["channels"],
           "FOLIANT_POOLS": addrs["pools"], "FOLIANT_TOKEN": addrs["token"], "PROVIDER_KEY": PROVIDER,
           "FOLIANT_STATE_DIR": str(tmp_path), "FOLIANT_POOL_SALT": str(_salt[0]), **extra}
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
    assert tap.give(first, "test")["transactions"]
    second = Account.create().address
    with pytest.raises(TapError) as e:
        tap.give(second, "test")
    assert e.value.status == 503 and "dry" in str(e.value)
    assert L.token(addrs["token"]).functions.balanceOf(second).call() == 0
    assert tap.status()["given"] == 1
    # refill the tap so later tests have gas
    d = Account.from_key(DEPLOYER)
    tx = {"to": Account.from_key(TAP_KEY).address, "value": int(bal * 0.6), "gas": 21_000, "gasPrice": L.w3.eth.gas_price,
          "nonce": L.w3.eth.get_transaction_count(d.address), "chainId": L.chain_id}
    L.w3.eth.wait_for_transaction_receipt(L.w3.eth.send_raw_transaction(d.sign_transaction(tx).raw_transaction))


# ---------------------------------------------------------------- TAP-AUDIT-FINAL fixes F-1 .. F-4


def _state(tmp_path) -> dict:
    return json.loads(next(p for p in tmp_path.iterdir() if p.suffix == ".json").read_text())


def test_per_client_daily_cap(chain, tmp_path, monkeypatch):
    """F-1: one client gets per_client_day attempts a day however many addresses it brings; the refusal is
    a distinct 429, costs no RPC and charges nothing, other clients are unaffected, the cap survives a
    restart, and the state file holds a hash of the client, never the client itself."""
    from demo.tap import TapError
    L, _, _ = chain
    tap = _tap(chain, tmp_path, per_hour=10, per_client_day=2)
    for _ in range(2):
        assert tap.give(Account.create().address, "203.0.113.7")["transactions"]

    def no_rpc(*a, **k):
        raise AssertionError("an RPC call was made for a request the limits refuse")

    monkeypatch.setattr(L.w3.eth, "get_code", no_rpc)
    with pytest.raises(TapError) as e:
        tap.give(Account.create().address, "203.0.113.7")
    assert e.value.status == 429 and "client" in str(e.value)
    monkeypatch.undo()
    assert tap.status()["given"] == 2
    assert tap.give(Account.create().address, "203.0.113.8")["transactions"]  # someone else still can
    with pytest.raises(TapError) as e:
        _tap(chain, tmp_path, per_hour=10, per_client_day=2).give(Account.create().address, "203.0.113.7")
    assert "client" in str(e.value)
    raw = next(p for p in tmp_path.iterdir() if p.suffix == ".json").read_text()
    assert "203.0.113" not in raw
    import hashlib
    assert len(_state(tmp_path)["clients"][hashlib.sha256(b"203.0.113.7").hexdigest()]) == 2


def test_global_daily_cap(chain, tmp_path):
    """F-1: FOLIANT_TAP_PER_DAY bounds everyone together, so rotating clients are bounded per day."""
    from demo.tap import TapError
    tap = _tap(chain, tmp_path, per_hour=10, per_day=2)
    tap.give(Account.create().address, "a")
    tap.give(Account.create().address, "b")
    with pytest.raises(TapError) as e:
        tap.give(Account.create().address, "c")
    assert e.value.status == 429 and "day" in str(e.value)
    assert tap.status()["given"] == 2


def test_http_client_is_peer_unless_proxy_trusted(chain, tmp_path):
    """F-1 at the HTTP layer: X-Forwarded-For is ignored unless FOLIANT_TRUST_PROXY=1 (so a visitor cannot
    mint themselves fresh clients with a header); when trusted, its last entry names the client."""
    from fastapi.testclient import TestClient
    c = TestClient(_app(chain, tmp_path / "peer"))
    for i in range(3):  # default FOLIANT_TAP_PER_CLIENT_DAY=3; every request is from the test client's peer
        r = c.post("/tap", json={"address": Account.create().address}, headers={"X-Forwarded-For": f"10.0.0.{i}"})
        assert r.status_code == 200, r.text
    r = c.post("/tap", json={"address": Account.create().address}, headers={"X-Forwarded-For": "10.0.0.99"})
    assert r.status_code == 429 and "client" in r.json()["error"]

    c = TestClient(_app(chain, tmp_path / "proxied", FOLIANT_TRUST_PROXY="1"))
    for _ in range(3):
        assert c.post("/tap", json={"address": Account.create().address}, headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 200
    r = c.post("/tap", json={"address": Account.create().address}, headers={"X-Forwarded-For": "10.0.0.1"})
    assert r.status_code == 429 and "client" in r.json()["error"]
    assert c.post("/tap", json={"address": Account.create().address}, headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 200
    # a client-supplied first entry does not help it: only the entry our proxy appended (the last) counts
    r = c.post("/tap", json={"address": Account.create().address}, headers={"X-Forwarded-For": "10.0.0.5, 10.0.0.1"})
    assert r.status_code == 429 and "client" in r.json()["error"]
    # nor does a second header line: a proxy that adds its own line leaves the visitor's first (XY-1)
    import httpx
    raw = [(b"content-type", b"application/json"), (b"x-forwarded-for", b"10.0.0.9"),
           (b"x-forwarded-for", b"10.0.0.1")]
    body = json.dumps({"address": Account.create().address}).encode()
    r = c.send(httpx.Request("POST", c.base_url.join("/tap"), headers=raw, content=body))
    assert r.status_code == 429 and "client" in r.json()["error"]


def test_rpc_error_on_send_with_tx_in_pool_counts_as_broadcast(chain, tmp_path, monkeypatch):
    """F-2 (A-3 = B-1): web3 retries eth_sendRawTransaction when a response is lost, and the retry's error
    ("already known" / "nonce too low") must not be read as "nothing in flight". Simulated by sending the
    raw transaction twice, so the second answer is the node's genuine error for a transaction it holds:
    the visitor is told 502 and the address is spent, not released. A genuine rejection with nothing in the
    pool still releases the address."""
    from demo.tap import TapError
    from web3.exceptions import Web3RPCError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path)
    real = L.w3.eth.send_raw_transaction

    def lost_response_then_retry(raw):
        real(raw)
        return real(raw)  # the retry: the node already has (or has mined) it and answers with an error

    monkeypatch.setattr(L.w3.eth, "send_raw_transaction", lost_response_then_retry)
    visitor = Account.create().address
    with pytest.raises(TapError) as e:
        tap.give(visitor, "test")
    assert e.value.status == 502 and "accepted by the node" in str(e.value)
    monkeypatch.undo()
    assert L.token(addrs["token"]).functions.balanceOf(visitor).call() == 1000 * 10 ** 6  # the mint went once
    assert L.w3.eth.get_balance(visitor) == 0
    with pytest.raises(TapError) as e:
        tap.give(visitor, "test")  # spent: no second mint
    assert e.value.status == 429
    assert L.token(addrs["token"]).functions.balanceOf(visitor).call() == 1000 * 10 ** 6
    rec = _state(tmp_path)["given"][visitor.lower()]
    assert rec.startswith("failed:") and "accepted by the node" in rec and "0x" in rec  # names the mint's hash

    def refused(raw):
        raise Web3RPCError("simulated: insufficient funds for gas * price + value")

    monkeypatch.setattr(L.w3.eth, "send_raw_transaction", refused)
    other = Account.create().address
    with pytest.raises(TapError) as e:
        tap.give(other, "test")
    assert e.value.status == 502 and "rejected" in str(e.value)
    monkeypatch.undo()
    assert other.lower() not in _state(tmp_path)["given"]  # nothing in flight: released
    assert tap.give(other, "test")["transactions"]


def test_unconfirmed_transaction_is_recorded_and_named(chain, tmp_path, monkeypatch):
    """F-3 (A-2 = B-3): a transaction that outlives the receipt window leaves the visitor recorded
    unconfirmed:<hash> (not failed:) and a stuck record; later visitors, on this and on a fresh instance,
    are refused 503 naming the hash; once the nonce clears the record is dropped and the tap serves again."""
    import demo.tap as tapmod
    from demo.tap import TapError
    L, addrs, _ = chain
    monkeypatch.setattr(tapmod, "RECEIPT_TIMEOUT", 1)
    monkeypatch.setattr(tapmod, "RECEIPT_POLL", 0.2)  # else one 2 s poll outlasts the 1 s timeout (Y-1)
    tap = _tap(chain, tmp_path)
    w3 = L.w3
    visitor = Account.create().address
    w3.provider.make_request("evm_setAutomine", [False])
    try:
        with pytest.raises(TapError) as e:
            tap.give(visitor, "test")
        assert e.value.status == 504
        s = _state(tmp_path)
        rec = s["given"][visitor.lower()]
        assert rec.startswith("unconfirmed:0x")
        h = rec.split(":", 1)[1]
        assert h in str(e.value)
        assert s["stuck"]["hash"] == h and s["stuck"]["nonce"] == w3.eth.get_transaction_count(tap.address, "latest")
        assert s["stuck"]["since"] == pytest.approx(time.time(), abs=10)
        for t in (tap, _tap(chain, tmp_path)):  # in-flight refusal names the hash, from the file too
            with pytest.raises(TapError) as e:
                t.give(Account.create().address, "test")
            assert e.value.status == 503 and h in str(e.value) and str(s["stuck"]["nonce"]) in str(e.value)
        assert tap.status()["given"] == 1  # the refused visitors were not charged
    finally:
        w3.provider.make_request("evm_setAutomine", [True])
        w3.provider.make_request("evm_mine", [])
    assert L.token(addrs["token"]).functions.balanceOf(visitor).call() == 1000 * 10 ** 6  # it landed after all
    assert tap.give(Account.create().address, "test")["transactions"]  # nonce cleared: serving again
    s = _state(tmp_path)
    assert s["stuck"] is None
    assert s["given"][visitor.lower()] == rec  # the visitor's record is left for the operator to reconcile
    with pytest.raises(TapError) as e:
        tap.give(visitor, "test")
    assert e.value.status == 429


def test_receipt_poll_latency(chain, tmp_path, monkeypatch):
    """F-4 (B-2): receipts are polled every 2 s, not web3's default 0.1 s."""
    L, _, _ = chain
    tap = _tap(chain, tmp_path)
    real = L.w3.eth.wait_for_transaction_receipt
    seen = []

    def spy(h, **kw):
        seen.append(kw)
        return real(h, **kw)

    monkeypatch.setattr(L.w3.eth, "wait_for_transaction_receipt", spy)
    assert tap.give(Account.create().address, "test")["transactions"]
    assert len(seen) == 2 and all(kw.get("poll_latency") == 2 for kw in seen), seen


# ---------------------------------------------------------------- TAP-AUDIT-FINAL fixes F-5 .. F-11


def _state_file(chain, tmp_path):
    L, _, _ = chain
    return tmp_path / f"foliant-tap-{L.chain_id}-{Account.from_key(TAP_KEY).address.lower()}.json"


def test_stale_balance_cache_cannot_mislead_give(chain, tmp_path, monkeypatch):
    """F-5 (A-7): the 30 s balance figure serves status() only. A stale-high one — left by this
    instance's own /chain, or by a second instance spending the same wallet — must not let give() mint
    and then fail the AVAX transfer: give() reads the balance fresh on every attempt."""
    from demo.tap import TapError
    L, addrs, _ = chain
    bal = L.w3.eth.get_balance(Account.from_key(TAP_KEY).address)
    tap = _tap(chain, tmp_path, avax_wei=bal + 1)     # more than the wallet holds
    tap._balance_cache = (time.time(), bal * 100)     # what a stale status() read leaves behind
    visitor = Account.create().address
    with pytest.raises(TapError) as e:
        tap.give(visitor, "test")
    assert e.value.status == 503 and "dry" in str(e.value)
    assert tap.status()["given"] == 0
    assert L.token(addrs["token"]).functions.balanceOf(visitor).call() == 0  # no mint was broadcast
    ok = _tap(chain, tmp_path)                        # and a healthy give asks the chain, not the cache
    ok._balance_cache = (time.time(), bal * 100)
    reads = []
    real = L.w3.eth.get_balance
    monkeypatch.setattr(L.w3.eth, "get_balance", lambda *a, **k: (reads.append(a), real(*a, **k))[1])
    assert ok.give(Account.create().address, "test")["transactions"]
    assert reads, "give() trusted status()'s cached balance"


def test_bad_state_file_fails_closed(chain, tmp_path):
    """F-6 (A-5 = B-4): only the shape we write is a state file. Anything else is corruption: refuse
    with a 503 naming the file rather than serving a half-understood state."""
    from demo.tap import TapError
    tap = _tap(chain, tmp_path)
    tap.give(Account.create().address, "test")
    p = _state_file(chain, tmp_path)
    for bad in ("not json at all", "[1, 2, 3]", '"a string"', '{"given": [1]}', '{"given": {"0xa": 5}}',
                '{"recent": "soon"}', '{"clients": {"x": 3}}', '{"recent": ["soon"]}',
                '{"stuck": {"nonce": 1}}', '{"version": 99}',
                # the values, not only the keys: each of these used to load and then raise a TypeError
                # at the stuck-record comparison, i.e. a 500 on every later request (D1)
                '{"stuck": {"nonce": "7", "hash": "0xa", "since": 1}}',
                '{"stuck": {"nonce": null, "hash": "0xa", "since": 1}}',
                '{"stuck": {"nonce": true, "hash": "0xa", "since": 1}}',
                '{"stuck": {"nonce": 7, "hash": null, "since": 1}}',
                '{"stuck": {"nonce": 7, "hash": "0xa", "since": "soon"}}',
                '{"stuck": {"nonce": 7, "hash": "0xa", "since": Infinity}}'):
        p.write_text(bad)
        with pytest.raises(TapError) as e:  # a fresh instance will not start on it
            _tap(chain, tmp_path)
        assert e.value.status == 503 and p.name in str(e.value), bad
        with pytest.raises(TapError) as e:  # nor does a running one serve from memory
            tap.give(Account.create().address, "test")
        assert e.value.status == 503 and p.name in str(e.value), bad


def test_absent_state_file_resets_the_tap(chain, tmp_path):
    """F-6: deleting the file under a running server resets it, rather than the next write re-creating
    it from whatever this instance still held in memory."""
    from demo.tap import TapError
    tap = _tap(chain, tmp_path, per_hour=2)
    tap.give(Account.create().address, "test")
    tap.give(Account.create().address, "test")
    with pytest.raises(TapError) as e:
        tap.give(Account.create().address, "test")
    assert e.value.status == 429
    _state_file(chain, tmp_path).unlink()
    assert tap.give(Account.create().address, "test")["transactions"]
    assert tap.status()["given"] == 1 and len(_state(tmp_path)["recent"]) == 1


def test_old_format_state_file_loads(chain, tmp_path):
    """F-6: "version" is written now, but a file from before it must still load and still be believed."""
    from demo.tap import TapError
    old = Account.create().address.lower()
    _state_file(chain, tmp_path).write_text(json.dumps({"given": {old: "funded"}, "recent": [time.time()]}))
    tap = _tap(chain, tmp_path)
    assert tap.status()["given"] == 1
    with pytest.raises(TapError) as e:
        tap.give(old, "test")
    assert e.value.status == 429                      # the old record still spends the address
    assert tap.give(Account.create().address, "test")["transactions"]
    s = _state(tmp_path)
    assert s["version"] == 1 and len(s["recent"]) == 2  # upgraded in place on the next write


def test_unwritable_state_at_the_charge_point_releases_the_address(chain, tmp_path, monkeypatch):
    """F-6 (B-5): when the charge cannot be persisted nothing is broadcast, so the address must not be
    left spent in this instance's memory for the life of the process."""
    from demo.tap import TapError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path)
    visitor = Account.create().address
    real_save = tap._save

    def enospc():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tap, "_save", enospc)
    with pytest.raises(TapError) as e:
        tap.give(visitor, "test")
    assert e.value.status == 503 and "not writable" in str(e.value)
    monkeypatch.setattr(tap, "_save", real_save)
    assert tap.status()["given"] == 0
    assert L.token(addrs["token"]).functions.balanceOf(visitor).call() == 0
    assert tap.give(visitor, "test")["transactions"]  # released, not stranded
    assert len(_state(tmp_path)["recent"]) == 1       # and the refusal took no hourly slot


def test_make_app_survives_a_broken_tap(chain, tmp_path):
    """F-6: the tap is optional, so a state file it cannot read disables it and leaves the rest of the
    server up; without this, /infer went down with an optional feature."""
    from fastapi.testclient import TestClient
    d = tmp_path / "broken"
    d.mkdir()
    _state_file(chain, d).write_text("{oops")
    c = TestClient(_app(chain, d))
    assert c.get("/chain").json()["tap"] is None
    assert c.post("/tap", json={"address": Account.create().address}).status_code == 404
    assert c.post("/infer", content=b"hello").status_code == 402  # the paid endpoint is still served
    assert c.get("/health").json()["ok"] is True


def test_chain_endpoint_survives_a_balance_rpc_failure(chain, tmp_path, monkeypatch):
    """F-7 (A-6 = B-7): GET /chain is how an agent finds the contracts, so a failing balance read must
    not 500 it, and must not cost one retrying RPC call per request while the outage lasts."""
    import demo.tap as tapmod
    from fastapi.testclient import TestClient
    c = TestClient(_app(chain, tmp_path))
    assert c.get("/chain").json()["tap"]["balanceWei"] > 0
    calls = []

    def down(self):
        calls.append(self)
        raise ConnectionError("simulated RPC outage")

    monkeypatch.setattr(tapmod, "BALANCE_CACHE", 0)  # the warm figure has expired
    monkeypatch.setattr(tapmod.Tap, "balance", down)  # the app has its own ledger: patch every tap
    for _ in range(5):
        r = c.get("/chain")
        assert r.status_code == 200
        assert r.json()["tap"]["balanceWei"] > 0      # the last known figure, not an error
    assert len(calls) == 1, calls                     # the failure is cached for a few seconds
    cold = _tap(chain, tmp_path / "cold")             # never read successfully: null, not a 500
    assert cold.status()["balanceWei"] is None


def test_precompile_recipient_refused_before_anything_is_charged(chain, tmp_path, monkeypatch):
    """F-8 (A-4): a precompile has no code, so get_code passes it, but a plain 21,000-gas transfer to
    one burns the gas and delivers nothing. The reserved ranges — the EVM precompiles and the two
    Avalanche keeps for its stateful ones, which answer a call cleanly — are refused from the address
    alone, so a bot repeating the refusal costs no RPC round trip at all."""
    from demo.tap import TapError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path)

    def no_rpc(*a, **k):
        raise AssertionError("an RPC call was made for an address the range check refuses")

    monkeypatch.setattr(L.w3.eth, "get_code", no_rpc)
    for reserved in ("0x0000000000000000000000000000000000000001",   # ecrecover
                     "0x0000000000000000000000000000000000000008",   # ecPairing
                     "0x0100000000000000000000000000000000000002",   # Avalanche stateful precompiles,
                     "0x0200000000000000000000000000000000000000"):  # which pass an eth_call cleanly
        with pytest.raises(TapError) as e:
            tap.give(reserved, "test")
        assert e.value.status == 400 and "reserved" in str(e.value), reserved
        assert L.w3.eth.get_balance(reserved) == 0
        assert L.token(addrs["token"]).functions.balanceOf(reserved).call() == 0
    monkeypatch.undo()
    assert tap.status()["given"] == 0                 # nothing charged, not even the address
    assert tap.give(Account.create().address, "test")["transactions"]
    assert len(_state(tmp_path)["recent"]) == 1       # and no hourly slot was spent on the refusals


def test_probe_tells_a_bad_recipient_from_a_bad_node(chain, tmp_path, monkeypatch):
    """F-8 with D4: past the reserved ranges it is the eth_call probe that refuses a recipient which
    cannot take a plain 21,000-gas transfer — but a node that is restarting or has pruned its state is
    the node's fault, and the visitor must not be told their address is bad."""
    from demo.tap import TapError
    from web3.exceptions import Web3RPCError
    L, _, _ = chain
    tap = _tap(chain, tmp_path)
    real_call = L.w3.eth.call
    monkeypatch.setattr(L.w3.eth, "call", lambda *a, **k: (_ for _ in ()).throw(
        Web3RPCError("{'code': -32603, 'message': 'EVM error PrecompileOOG'}")))
    with pytest.raises(TapError) as e:
        tap.give(Account.create().address, "test")
    assert e.value.status == 400 and "cannot receive" in str(e.value)
    monkeypatch.setattr(L.w3.eth, "call", lambda *a, **k: (_ for _ in ()).throw(
        Web3RPCError("{'code': -32603, 'message': 'missing trie node; node restarting'}")))
    visitor = Account.create().address
    with pytest.raises(TapError) as e:
        tap.give(visitor, "test")
    assert e.value.status == 502 and "chain unavailable" in str(e.value)  # not "your address is bad"
    monkeypatch.setattr(L.w3.eth, "call", real_call)
    assert tap.status()["given"] == 0                 # neither refusal charged anything
    assert tap.give(visitor, "test")["transactions"]  # and the address is still eligible


def test_tokens_only_tap_still_probes_the_recipient(chain, tmp_path, monkeypatch):
    """D5: with FOLIANT_TAP_AVAX=0 there is no transfer to lose, but a mint to a precompile strands the
    tokens where nothing can ever move them, so the recipient is still checked."""
    from demo.tap import TapError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path, avax_wei=0)
    precompile = "0x0000000000000000000000000000000000000001"
    with pytest.raises(TapError) as e:
        tap.give(precompile, "test")
    assert e.value.status == 400
    assert L.token(addrs["token"]).functions.balanceOf(precompile).call() == 0
    seen = []
    real_call = L.w3.eth.call
    monkeypatch.setattr(L.w3.eth, "call", lambda *a, **k: (seen.append(a), real_call(*a, **k))[1])
    assert tap.give(Account.create().address, "test")["tokens"]
    assert any(isinstance(a[0], dict) and a[0].get("gas") == 21_000 for a in seen), seen  # probed at 0


def test_a_non_rpc_failure_is_a_clean_status(chain, tmp_path, monkeypatch):
    """D2: narrowing what counts as "chain unavailable" must not let a bug of ours, a full disk or an
    ENOLCK reach the visitor as a traceback. An RPC error still says so; anything else is a clean 503."""
    import demo.tap as tapmod
    from fastapi.testclient import TestClient
    from demo.tap import TapError
    from web3.exceptions import Web3RPCError
    L, _, _ = chain
    tap = _tap(chain, tmp_path)

    def bug(*a, **k):
        raise TypeError("a bug of ours, not the chain's fault")

    monkeypatch.setattr(L.w3.eth, "get_code", bug)
    with pytest.raises(TapError) as e:
        tap.give(Account.create().address, "test")
    assert e.value.status == 503 and "temporarily unavailable" in str(e.value)
    assert tap.status()["given"] == 0
    monkeypatch.setattr(L.w3.eth, "get_code", lambda *a, **k: (_ for _ in ()).throw(Web3RPCError("no")))
    with pytest.raises(TapError) as e:
        tap.give(Account.create().address, "test")
    assert e.value.status == 502 and "chain unavailable" in str(e.value)
    monkeypatch.undo()
    c = TestClient(_app(chain, tmp_path / "http"))    # and over HTTP it is a status, never a 500
    monkeypatch.setattr(tapmod.Tap, "_load", bug)
    r = c.post("/tap", json={"address": Account.create().address})
    assert r.status_code == 503 and r.json()["error"]


def test_a_failed_final_save_still_reports_the_funding(chain, tmp_path, monkeypatch):
    """D2: the visitor's AVAX and tokens are on chain by the time the last state write happens, so a
    failing write must not turn a successful give into a 500 with no transaction hashes. The record on
    disk stays "pending", which still refuses a second attempt."""
    from demo.tap import TapError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path)
    real_save = tap._save
    n = {"i": 0}

    def fails_after_the_charge():
        n["i"] += 1
        if n["i"] == 1:
            return real_save()
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tap, "_save", fails_after_the_charge)
    visitor = Account.create().address
    out = tap.give(visitor, "test")
    assert len(out["transactions"]) == 2
    assert L.w3.eth.get_balance(visitor) == Web3.to_wei("0.02", "ether")
    assert L.token(addrs["token"]).functions.balanceOf(visitor).call() == 1000 * 10 ** 6
    assert _state(tmp_path)["given"][visitor.lower()] == "pending"
    monkeypatch.setattr(tap, "_save", real_save)
    with pytest.raises(TapError) as e:
        _tap(chain, tmp_path).give(visitor, "test")   # "pending" still spends the address
    assert e.value.status == 429


def test_file_lock_does_not_leak_a_descriptor(chain, tmp_path, monkeypatch):
    """F-9 (B-6): both _FileLock classes closed the fd only on BlockingIOError, so any other flock
    failure leaked one per attempt until the process ran out of descriptors."""
    import fcntl as fcntlmod
    from demo.tap import TapError
    from foliant.chain import ChainGate
    tap = _tap(chain, tmp_path)

    def enolck(fd, op):
        raise OSError(37, "No locks available")

    def open_fds():
        return len(os.listdir("/proc/self/fd"))

    monkeypatch.setattr(fcntlmod, "flock", enolck)  # the same module object both files imported
    before = open_fds()
    for _ in range(40):
        with pytest.raises(TapError) as e:  # a clean 503, not the raw OSError as a 500 (D2)
            tap.give(Account.create().address, "test")
        assert e.value.status == 503
    assert open_fds() - before <= 2, (before, open_fds())
    gate_lock = ChainGate._FileLock(tmp_path / "gate.json")
    before = open_fds()
    for _ in range(40):
        with pytest.raises(OSError):
            with gate_lock:
                pass
    assert open_fds() - before <= 2, (before, open_fds())


def test_address_prefix_is_required_and_normalised(chain, tmp_path):
    """F-10 (A-8 = B-8): Web3.is_address accepts bare hex, and slicing [2:] then ate two nibbles — a
    correct address was refused "bad checksum" (or, lowercase, raised out of give()). The prefix is now
    required, and a 0X one is normalised rather than mangled."""
    from demo.tap import TapError
    tap = _tap(chain, tmp_path)
    v = Account.create().address                      # checksummed, mixed case
    for bare in (v[2:], v[2:].lower()):
        with pytest.raises(TapError) as e:
            tap.give(bare, "test")
        assert e.value.status == 400 and "0x" in str(e.value) and "checksum" not in str(e.value), bare
    assert tap.status()["given"] == 0
    assert tap.give("0X" + v[2:], "test")["address"] == v          # 0X accepted, checksum honoured
    w = Account.create().address
    with pytest.raises(TapError) as e:
        tap.give("0X" + w[2:].swapcase(), "test")     # a real typo is still caught through a 0X prefix
    assert e.value.status == 400 and "checksum" in str(e.value)


def test_oversized_body_refused(chain, tmp_path):
    """F-11 (A-8): POST /tap takes one address; anything over 1 KiB is refused before it is parsed."""
    from fastapi.testclient import TestClient
    c = TestClient(_app(chain, tmp_path))
    assert c.post("/tap", content=b"x" * 2048).status_code == 413
    v = Account.create().address
    padded = json.dumps({"address": v, "pad": "y" * 2000})
    r = c.post("/tap", content=padded, headers={"content-type": "application/json"})
    assert r.status_code == 413 and "1024" in r.json()["error"]
    assert c.post("/tap", json={"address": v}).status_code == 200   # the refusals charged nothing
    assert c.get("/chain").json()["tap"]["given"] == 1


def test_chunked_oversized_body_is_not_buffered(chain, tmp_path):
    """F-11 with D3: a body with no content-length is read a chunk at a time and abandoned once it
    passes the bound, rather than buffered in full and only then refused. TestClient hands the app one
    already-buffered body, so the app is driven directly here and the chunks it pulls are counted."""
    import asyncio
    app = _app(chain, tmp_path)
    pulled, sent = [], []

    async def receive():                 # half a megabyte, if anyone reads it all
        pulled.append(1)
        return {"type": "http.request", "body": b"x" * 1024, "more_body": len(pulled) < 512}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"}, "http_version": "1.1",
             "method": "POST", "path": "/tap", "raw_path": b"/tap", "root_path": "", "scheme": "http",
             "query_string": b"", "client": ("127.0.0.1", 5555), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")]}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    assert len(pulled) < 16, len(pulled)  # stopped just past 1 KiB, not after 512


def test_probe_message_matching_blames_the_right_party(chain, tmp_path, monkeypatch):
    """D4 residue: a node saying it is reverting to a snapshot, or that the block ran out of gas room,
    is the node's trouble (502), not the visitor's address (400)."""
    from web3.exceptions import Web3RPCError
    from demo.tap import TapError
    L, _, _ = chain
    tap = _tap(chain, tmp_path)

    def probe_raises(msg):
        def call(*a, **k):
            raise Web3RPCError({"code": -32603, "message": msg})
        return call

    node_faults = ["node is reverting to the last snapshot", "the block ran out of gas room",
                   "missing trie node; node restarting"]
    for msg in node_faults:
        monkeypatch.setattr(L.w3.eth, "call", probe_raises(msg))
        with pytest.raises(TapError) as e:
            tap.give(Account.create().address, "c")
        assert e.value.status == 502, msg
        monkeypatch.undo()
    for msg in ["EVM error PrecompileOOG", "execution reverted", "EVM error Revert", "invalid opcode: INVALID"]:
        monkeypatch.setattr(L.w3.eth, "call", probe_raises(msg))
        with pytest.raises(TapError) as e:
            tap.give(Account.create().address, "c")
        assert e.value.status == 400, msg
        monkeypatch.undo()
    assert tap.status()["given"] == 0  # nothing charged either way


def test_infinite_timestamps_are_a_corrupt_file(chain, tmp_path):
    """D7: an Infinity in recent or a client's list never expires, so it would hold a slot for ever."""
    from demo.tap import Tap, TapError
    L, addrs, _ = chain
    tap = _tap(chain, tmp_path)
    tap.give(Account.create().address, "c")  # create the file
    path = _state_file(chain, tmp_path)
    good = json.loads(path.read_text())
    for bad in ({**good, "recent": [float("inf")]},
                {**good, "clients": {"h" * 64: [float("inf")]}},
                {**good, "recent": ["1"]},
                {**good, "clients": {"h" * 64: {"1": 2}}}):
        path.write_text(json.dumps(bad))
        with pytest.raises(TapError) as e:
            Tap(L, TAP_KEY, addrs["token"], avax_wei=1, tokens=1, per_hour=1, state_dir=str(tmp_path))
        assert e.value.status == 503 and "unreadable" in str(e.value)


def test_server_restarts_without_recreating_its_pool(chain, tmp_path):
    """The pool id is determined by (coordinator, salt), so a second start would try to create the
    same pool and die on PoolExists — which made the deployed server unrestartable until it reused
    an existing pool instead."""
    from fastapi.testclient import TestClient
    salt = str(_salt[0] + 1000)
    first = TestClient(_app(chain, tmp_path, FOLIANT_POOL_SALT=salt))
    pid = first.get("/chain").json()["poolId"]
    # a second server with the same provider key and salt, which is what a restart is
    second = TestClient(_app(chain, tmp_path, FOLIANT_POOL_SALT=salt))
    assert second.get("/chain").json()["poolId"] == pid
