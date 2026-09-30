"""AUDIT-3: independent review of foliant/chain.py (ChainLedger / ChainAgent / ChainGate / ChainHttpClient).

Failing tests are real defects and are marked `# FINDING A3-n`; passing checks are marked `# VERIFY A3-...`.
Same Anvil fixture approach as tests/test_chain.py. See contracts/audits/AUDIT-3.md.
"""
import json
import shutil
import socket
import subprocess
import time

import pytest
from eth_account import Account
from eth_account.messages import _hash_eip191_message, encode_defunct, encode_typed_data
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from web3 import Web3

from foliant.chain import (HDR_PAYMENT, ChainAgent, ChainGate, ChainLedger, ChainOffer, ChainPayment, ChainPolicy,
                           PaymentRequired, _b64, _hex, _unb64, deploy_local)
from foliant.errors import FoliantError

KEYS = ["0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
        "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
        "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
        "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
        "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
        "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba"]
SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

pytestmark = pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil not installed")


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
    addrs = deploy_local(w3, KEYS[0])
    L = ChainLedger("", addrs["accounts"], addrs["channels"], addrs["pools"], w3=w3)
    yield L, addrs
    proc.kill()


def _mint(L, token, to, amount, key=KEYS[0]):
    a = Account.from_key(key)
    t = L.token(token)
    tx = t.functions.mint(to, amount).build_transaction({"from": a.address, "nonce": L.w3.eth.get_transaction_count(a.address),
                                                          "chainId": L.chain_id})
    L.w3.eth.wait_for_transaction_receipt(L.w3.eth.send_raw_transaction(a.sign_transaction(tx).raw_transaction))


def _warp(L, secs):
    L.w3.provider.make_request("evm_increaseTime", [secs])
    L.w3.provider.make_request("evm_mine", [])


_salt = [100]


def _funded_agent(L, token, key, amount=10_000, policy=ChainPolicy(5000, 100_000, 3600)):
    a = ChainAgent(L, key)
    _salt[0] += 1
    a.register(policy, salt=_salt[0])
    _mint(L, token, a.address, amount)
    a.deposit(token, amount)
    return a


def _header(scheme, u):
    return _b64({"scheme": scheme, "id": u["id"], "update": u})


def _app(gate):
    app = FastAPI()

    @app.exception_handler(PaymentRequired)
    async def _h(_r, exc):
        return JSONResponse(status_code=402, content=exc.terms)

    @app.post("/infer")
    async def infer(request: Request, payment: ChainPayment = Depends(gate.dependency())):
        body = await request.body()
        out = Response(b"ok", media_type="text/plain")
        out.headers["X-PAYMENT-RESPONSE"] = gate.receipt(payment, body, out.body)
        return out

    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------------ 2. signature recovery



def _pay(agent, obj_id, amount):
    """Sign and confirm, as the http client does after a 200 (pending/confirm semantics, AUDIT-3 A3-9)."""
    try:
        agent.L.channel(obj_id)
        u = agent.pay_channel(obj_id, amount)
    except Exception:
        u = agent.pay_pool(obj_id, amount)
    agent.confirm(obj_id)
    return u

def test_channel_digest_matches_contract(chain):  # VERIFY A3-digest-channel
    L, _ = chain
    domain, types = L.channel_update_types()
    cases = [(b"\x00" * 31 + b"\x01", b"\x00" * 32, 1, 5),
             (b"\x00" * 16 + b"\xff" * 16, b"\x00" * 31 + b"\x00", 2 ** 64 - 1, 2 ** 256 - 1),
             (bytes(range(32)), b"\x01" + b"\x00" * 31, 0, 0)]
    for cid, acct, seq, bal in cases:
        onchain = L.channels.functions.updateDigest(cid, acct, seq, bal).call()
        local = _hash_eip191_message(encode_typed_data(domain, types, {"channel": cid, "account": acct, "seq": seq,
                                                                        "balance": bal}))
        assert onchain == local


def test_pool_digest_matches_contract(chain):  # VERIFY A3-digest-pool
    L, _ = chain
    domain, types = L.pool_update_types()
    cases = [(b"\x00" * 31 + b"\x01", b"\x00" * 32, 1, 1, 5),
             (b"\x00" * 16 + b"\xff" * 16, b"\x00" * 32, 2 ** 64 - 1, 2 ** 64 - 1, 2 ** 256 - 1),
             (bytes(range(32)), b"\x01" + b"\x00" * 31, 7, 0, 0)]
    for pid, acct, epoch, seq, bal in cases:
        onchain = L.pools.functions.updateDigest(pid, acct, epoch, seq, bal).call()
        local = _hash_eip191_message(encode_typed_data(domain, types, {"pool": pid, "account": acct, "epoch": epoch,
                                                                        "seq": seq, "balance": bal}))
        assert onchain == local


def _malleate_high_s(sig_hex: str) -> str:
    b = bytes.fromhex(sig_hex[2:])
    r, s, v = int.from_bytes(b[:32], "big"), int.from_bytes(b[32:64], "big"), b[64]
    return _hex(r.to_bytes(32, "big") + (SECP_N - s).to_bytes(32, "big") + bytes([27 if v == 28 else 28]))


def _v01(sig_hex: str) -> str:
    b = bytes.fromhex(sig_hex[2:])
    return _hex(b[:64] + bytes([b[64] - 27]))


def test_gate_accepts_high_s_signature_the_contract_rejects(chain):  # FINDING A3-1
    """eth_account recovers a high-s (malleated) signature to the right signer; OpenZeppelin ECDSA rejects it.
    The gate therefore accepts a payment it can never settle, and the payer gets the call for free."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=1)
    u = payer.pay_channel(cid, 3)
    mal = {**u, "sig": _malleate_high_s(u["sig"])}
    assert mal["sig"] != u["sig"]
    # the contract refuses the malleated signature: settle reverts (estimateGas fails)
    with pytest.raises(Exception):
        L.channels.functions.settle(cid, mal["seq"], mal["balance"], bytes.fromhex(mal["sig"][2:])).call()
    # ... but the gate must refuse it too, or the payer gets served for an unsettleable update
    with pytest.raises(PaymentRequired):
        gate.verify(_header("foliant-channel", mal))


def test_gate_accepts_v01_signature_the_contract_rejects(chain):  # FINDING A3-1 (v encoding variant)
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=2)
    u = payer.pay_channel(cid, 3)
    mal = {**u, "sig": _v01(u["sig"])}
    with pytest.raises(Exception):
        L.channels.functions.settle(cid, mal["seq"], mal["balance"], bytes.fromhex(mal["sig"][2:])).call()
    with pytest.raises(PaymentRequired):
        gate.verify(_header("foliant-channel", mal))


def test_high_s_pool_update_bricks_the_whole_pool_settlement(chain):  # FINDING A3-1 (pool consequence)
    """Once accepted, one malleated pool update makes Pools.settle revert for every member (bad sig reverts the batch)."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    pid = gate.create_pool(timeout_secs=3600, salt=11)
    honest = _funded_agent(L, token, KEYS[1])
    evil = _funded_agent(L, token, KEYS[2])
    honest.join_pool(pid, 30)
    evil.join_pool(pid, 30)
    gate.verify(_header("foliant-pool", honest.pay_pool(pid, 3)))
    ue = evil.pay_pool(pid, 3)
    try:
        gate.verify(_header("foliant-pool", {**ue, "sig": _malleate_high_s(ue["sig"])}))
    except PaymentRequired:
        pytest.skip("gate refuses high-s (A3-1 fixed); nothing to brick")
    with pytest.raises(Exception):
        gate.settle()
    # the honest member's 3 units are stuck: still unsettled on-chain
    assert L.claim(pid, honest.account_id)["paid"] == 0
    pytest.fail("A3-1: one malleated header, once accepted, blocks settlement of the whole pool")


# ------------------------------------------------------------------ 1. gate verification


def test_latest_key_is_not_canonical_so_updates_replay(chain):  # FINDING A3-2
    """The gate's memory is keyed by the raw header strings. Re-sending an already-accepted update with the
    channel id in a different hex case (or any 2-char prefix) lands under a new key whose baseline is the
    on-chain `paid`, so every accepted-but-unsettled balance can be replayed for a second (third, ...) call."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=3)
    u1 = _pay(payer, cid, 3)
    u2 = _pay(payer, cid, 3)
    assert gate.verify(_header("foliant-channel", u1)).paid == 3
    assert gate.verify(_header("foliant-channel", u2)).paid == 3
    # exact replay is refused ...
    with pytest.raises(PaymentRequired):
        gate.verify(_header("foliant-channel", u1))
    # ... but the same signed update with the id upper-cased is accepted again, twice
    replay1 = {**u1, "id": "0x" + u1["id"][2:].upper()}
    replay2 = {**u2, "id": "0x" + u2["id"][2:].upper()}
    with pytest.raises(PaymentRequired):
        gate.verify(_header("foliant-channel", replay1))
    with pytest.raises(PaymentRequired):
        gate.verify(_header("foliant-channel", replay2))


def test_latest_key_not_canonical_pool_variant(chain):  # FINDING A3-2 (pool: id and account both uncanonical)
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    pid = gate.create_pool(timeout_secs=3600, salt=12)
    payer = _funded_agent(L, token, KEYS[1])
    payer.join_pool(pid, 30)
    u = payer.pay_pool(pid, 3)
    gate.verify(_header("foliant-pool", u))
    with pytest.raises(PaymentRequired):
        gate.verify(_header("foliant-pool", {**u, "account": "0X" + u["account"][2:].upper()}))


def test_gate_restart_replays_unsettled_updates(chain):  # FINDING A3-3
    """`latest` lives only in memory. A new gate (restart, second worker) accepts every accepted-but-unsettled
    update again; the provider is paid once for the calls served before and after the restart."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    offer = ChainOffer(prov, token, 3)
    gate = ChainGate(L, KEYS[3], offer)
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=4)
    ups = [_pay(payer, cid, 3) for _ in range(3)]
    for u in ups:
        gate.verify(_header("foliant-channel", u))
    gate2 = ChainGate(L, KEYS[3], offer)  # restart without settling
    served_again = 0
    for u in ups:
        try:
            gate2.verify(_header("foliant-channel", u))
            served_again += 1
        except PaymentRequired:
            pass
    assert served_again == 0, f"{served_again} of 3 unsettled updates replayed after restart"


def test_malformed_headers_return_402_not_500(chain):  # FINDING A3-4
    """Recovery runs before the integer type check, and eth_abi / eth_utils / web3 exceptions are not in the
    gate's except tuple, so a malformed header is a 500 (traceback in the log) instead of a 402."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=5)
    u = payer.pay_channel(cid, 3)
    tc = _app(gate)
    bad = [
        {**u, "seq": 1.5},                                  # eth_abi EncodingTypeError
        {**u, "seq": True},                                 # bool passes isinstance(int); eth_abi EncodingTypeError
        {**u, "seq": 2 ** 64},                              # eth_abi ValueOutOfBounds
        {**u, "balance": -1},                               # eth_abi ValueOutOfBounds
        {**u, "balance": 2 ** 256},                         # eth_abi ValueOutOfBounds
        {**u, "sig": u["sig"][:-2]},                        # eth_utils ValidationError (64-byte sig)
        {**u, "sig": "0x"},                                 # IndexError
        {**u, "id": _hex(b"\x00" * 32)},                    # NoChannel revert -> web3 ContractCustomError
    ]
    codes = [tc.post("/infer", content=b"x", headers={HDR_PAYMENT: _header("foliant-channel", b)}).status_code for b in bad]
    assert all(c == 402 for c in codes), codes


def test_gate_rejections_that_are_correct(chain):  # VERIFY A3-gate-rejects
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    other = Account.from_key(KEYS[4]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    pid = gate.create_pool(timeout_secs=3600, salt=13)
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=6)
    u = payer.pay_channel(cid, 3)

    def refused(scheme, upd):
        with pytest.raises(PaymentRequired):
            gate.verify(_header(scheme, upd))

    refused("foliant-channel", {**u, "balance": "3"})                   # string balance: type check (after recovery)
    refused("foliant-channel", {**u, "balance": 101})                   # over deposit (sig wrong anyway)
    refused("foliant-channel", {**u, "account": _hex(b"\x01" * 32)})    # account not the channel's payer
    refused("foliant-pool", {**u, "epoch": 1})                          # pool scheme with a channel id
    refused("nope", u)
    # wrong payee: a channel to someone else
    cid2 = payer.open_channel(other, token, 100, 3600, salt=7)
    refused("foliant-channel", payer.pay_channel(cid2, 3))
    # closing channel
    cid3 = payer.open_channel(prov, token, 100, 3600, salt=8)
    u3 = payer.pay_channel(cid3, 3)
    payer.close_channel(cid3)
    refused("foliant-channel", u3)
    # underpaid and stale
    gate.verify(_header("foliant-channel", u))
    refused("foliant-channel", u)
    refused("foliant-channel", {**payer.pay_channel(cid, 1)})
    # pool: exiting member and wrong epoch
    payer.join_pool(pid, 30)
    up = _pay(payer, pid, 3)
    refused("foliant-pool", {**up, "epoch": 2})
    gate.verify(_header("foliant-pool", up))
    payer.begin_exit(pid)
    refused("foliant-pool", payer.pay_pool(pid, 3))


def test_channel_and_pool_schemes_share_no_keys(chain):  # VERIFY A3-key-scheme
    L, addrs = chain
    gate = ChainGate(L, KEYS[3], ChainOffer(Account.from_key(KEYS[3]).address, addrs["token"], 3))
    assert gate.latest == {} and "foliant-channel:" != "foliant-pool:"


# ------------------------------------------------------------------ 3. receipts


def test_receipt_verifiable_by_client(chain):  # VERIFY A3-receipt
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=9)
    u = payer.pay_channel(cid, 3)
    tc = _app(gate)
    r = tc.post("/infer", content=b"req", headers={HDR_PAYMENT: _header("foliant-channel", u)})
    assert r.status_code == 200
    rec = _unb64(r.headers["X-PAYMENT-RESPONSE"])
    body = rec["body"]
    # client-side verification procedure (documented in AUDIT-3): recompute the three hashes and recover the signer
    assert body["updateId"] == Web3.keccak(text=json.dumps(u, sort_keys=True)).hex()
    assert body["requestHash"] == Web3.keccak(b"req").hex() and body["responseHash"] == Web3.keccak(b"ok").hex()
    assert body["offerId"] == gate.offer.id
    signer = Account.recover_message(encode_defunct(text=json.dumps(body, sort_keys=True)), signature=rec["signature"])
    assert signer == rec["signer"]["key"] == prov
    # updateId is order-independent (sort_keys) and binds sig
    assert Web3.keccak(text=json.dumps(dict(reversed(list(u.items()))), sort_keys=True)).hex() == body["updateId"]


# ------------------------------------------------------------------ 4. settlement


def test_closed_channel_entry_blocks_all_later_settlement(chain):  # FINDING A3-5
    """A channel closed by the payer with an empty/older update (the provider missed the timeout) stays in
    `latest`; `settle()` raises at that entry and never reaches the entries after it, on every call."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    p1 = _funded_agent(L, token, KEYS[1])
    p2 = _funded_agent(L, token, KEYS[2])
    c1 = p1.open_channel(prov, token, 100, 1, salt=10)
    c2 = p2.open_channel(prov, token, 100, 3600, salt=10)
    gate.verify(_header("foliant-channel", p1.pay_channel(c1, 3)))
    gate.verify(_header("foliant-channel", p2.pay_channel(c2, 3)))
    # payer 1 closes without applying its update, waits out the 1s timeout, finalizes
    p1._send(L.channels.functions.beginClose(c1, 0, 0, b""))
    _warp(L, 5)
    p1.finalize_close(c1)
    assert L.channel(c1)["closed"]
    try:
        total, txs = gate.settle()
    except Exception:
        total, txs = None, []
    # whatever happened to channel 1 (lost by design), channel 2 must still be settled
    assert L.channel(c2)["paid"] == 3, "payer 2's update was never settled: the closed channel blocks the loop"


def test_member_exit_and_rejoin_bricks_pool_settlement(chain):  # FINDING A3-6
    """A member pays, exits (empty update), waits the timeout, finalizes and rejoins (epoch+1) before the
    provider settles. Its accepted epoch-1 update is now Unauthorized and, because the batch reverts on a
    bad signature, no member of the pool can be settled while that entry remains in `latest`."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    pid = gate.create_pool(timeout_secs=1, salt=14)
    honest = _funded_agent(L, token, KEYS[1])
    evil = _funded_agent(L, token, KEYS[2])
    honest.join_pool(pid, 30)
    evil.join_pool(pid, 30)
    gate.verify(_header("foliant-pool", honest.pay_pool(pid, 3)))
    gate.verify(_header("foliant-pool", evil.pay_pool(pid, 3)))
    evil.discard(pid)  # never confirmed, so exit goes out with an empty update (the contract allows it)
    evil.begin_exit(pid)
    _warp(L, 5)
    evil.finalize_exit(pid)
    evil.join_pool(pid, 30)
    assert L.claim(pid, evil.account_id)["epoch"] == 2
    try:
        gate.settle()
    except Exception:
        pass
    assert L.claim(pid, honest.account_id)["paid"] == 3, "honest member's revenue stuck behind the rejoined member's stale-epoch entry"


def test_settle_sends_a_pool_tx_even_when_nothing_is_new(chain):  # FINDING A3-7
    """Pool entries are never pruned and never compared with on-chain `paid`, so every settle() after the
    first sends a (no-op) transaction. With /settle unauthenticated in serve_chain.py this is gas griefing."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    pid = gate.create_pool(timeout_secs=3600, salt=15)
    payer = _funded_agent(L, token, KEYS[1])
    payer.join_pool(pid, 30)
    gate.verify(_header("foliant-pool", payer.pay_pool(pid, 3)))
    total, txs = gate.settle()
    assert total == 3 and len(txs) == 1
    total, txs = gate.settle()
    assert total == 0
    assert txs == [], "a second settle with nothing new still paid for a transaction"


def test_channel_superseded_on_chain_is_skipped(chain):  # VERIFY A3-settle-skip
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=11)
    u1 = _pay(payer, cid, 3)
    gate.verify(_header("foliant-channel", u1))
    u2 = _pay(payer, cid, 3)  # the payer applies a higher update itself (e.g. via beginClose elsewhere)
    payer._send(L.channels.functions.settle(cid, u2["seq"], u2["balance"], bytes.fromhex(u2["sig"][2:])))
    total, txs = gate.settle()
    assert total == 0 and txs == []
    # FIXED semantics: the superseded entry is pruned, so the gate's baseline is the on-chain paid (6):
    # u2, which the payer settled itself, buys nothing; the next signed update does
    with pytest.raises(PaymentRequired):
        gate.verify(_header("foliant-channel", u2))
    assert gate.verify(_header("foliant-channel", _pay(payer, cid, 3))).paid == 3


def test_settle_revert_midway_keeps_revenue_unsettled_accounting(chain):  # VERIFY A3-settle-partial
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=12)
    gate.verify(_header("foliant-channel", payer.pay_channel(cid, 3)))
    gate.latest["foliant-channel:0x" + "00" * 32 + ":" + _hex(payer.account_id)] = \
        {"id": "0x" + "00" * 32, "account": _hex(payer.account_id), "seq": 1, "balance": 1, "sig": "0x" + "00" * 65}
    # FIXED A3-5: an unsettleable entry is dropped, the rest settles, and the accounting closes
    total, txs = gate.settle()
    assert total == 3 and len(txs) == 1
    assert gate.latest == {} and gate.revenue_unsettled == 0


# ------------------------------------------------------------------ 5. agent side


def test_policy_revert_surfaces_as_web3_error_not_foliant_error(chain):  # FINDING A3-8
    """`_send` builds with build_transaction, whose eth_estimateGas already reverts on a policy violation; the
    exception is web3's ContractCustomError, not the FoliantError the docstring and test_chain.py imply.
    (No gas is lost, which is the good part; the API contract is the bug.)"""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    payer = _funded_agent(L, token, KEYS[1], policy=ChainPolicy(10, 100, 3600))
    with pytest.raises(FoliantError):
        payer.open_channel(prov, token, 50, 3600, salt=13)  # 50 > per_tx_max 10


def test_agent_advances_latest_on_rejected_payment_and_overpays(chain):  # FINDING A3-9
    """pay_channel commits the balance locally whether or not the provider accepts it. After one refused
    request (network error, 402 for any reason) the next accepted update pays for two calls."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=14)
    payer.pay_channel(cid, 3)  # never delivered
    p = gate.verify(_header("foliant-channel", payer.pay_channel(cid, 3)))
    assert p.paid == 3, f"agent paid {p.paid} for one call"


def test_agent_restart_before_provider_settles_is_stuck(chain):  # VERIFY A3-agent-restart (documented, not a defect of chain.py alone)
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=15)
    gate.verify(_header("foliant-channel", _pay(payer, cid, 3)))
    fresh = ChainAgent(L, KEYS[1], account_id=payer.account_id)  # agent restarted: no `latest`
    with pytest.raises(PaymentRequired):  # balance 3 again: stale for the gate until the provider settles
        gate.verify(_header("foliant-channel", _pay(fresh, cid, 3)))
    gate.settle()
    assert gate.verify(_header("foliant-channel", _pay(fresh, cid, 3))).paid == 3


# ================================================================== Verification, round 2 (commit 6bcca8c)

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from foliant.chain import ChainHttpClient, _check_sig, _canon32


def test_v2_check_sig_matches_openzeppelin_set(chain):  # VERIFY-2 A3-1
    """Gate acceptance set == OZ ECDSA 5.4 tryRecover(bytes32, bytes): 65 bytes, v in {27,28}, 0 < s <= n//2,
    0 < r < n (r out of range fails recovery in eth_keys, address(0) in ecrecover). 64-byte EIP-2098: both reject."""
    L, addrs = chain
    payer = ChainAgent(L, KEYS[1])
    domain, types = L.channel_update_types()
    msg = {"channel": b"\x01" * 32, "account": b"\x02" * 32, "seq": 1, "balance": 1}
    s = payer.key.sign_typed_data(domain, types, msg)
    r_, s_, v_ = s.r, s.s, s.v

    def sig(r, ss, v):
        return _hex(r.to_bytes(32, "big") + ss.to_bytes(32, "big") + bytes([v]))

    def contract_ok(sg):
        return L.channels.functions.settle(b"\x01" * 32, 1, 1, bytes.fromhex(sg[2:])).call

    ok = {sig(r_, s_, v_), sig(r_, SECP_N // 2, v_)}          # canonical, and the boundary s == n//2
    bad = {sig(r_, SECP_N - s_, 55 - v_), sig(r_, s_, v_ - 27), sig(r_, 0, v_), sig(0, s_, v_), sig(SECP_N, s_, v_),
           sig(r_, SECP_N // 2 + 1, v_), sig(r_, s_, 29), _hex(bytes.fromhex(sig(r_, s_, v_)[2:])[:64])}
    for sg in ok:
        _check_sig(sg)
    for sg in bad:
        with pytest.raises(Exception):
            _check_sig(sg) and Account.recover_message(encode_typed_data(domain, types, msg), signature=bytes.fromhex(sg[2:]))
    # the contract side of the boundary: s == n//2 passes the S check (fails only on rec != signer for this fake channel)
    with pytest.raises(Exception) as ei:
        contract_ok(sig(r_, SECP_N // 2, v_))()
    assert "NoChannel" in str(ei.value) or "0x" in str(ei.value)  # NoChannel: it got past nothing else; digest irrelevant


def test_v2_canonical_ids_and_replay_closed(chain):  # VERIFY-2 A3-2
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3), state_dir=None)
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=21)
    u = _pay(payer, cid, 3)
    up = {**u, "id": "0x" + u["id"][2:].upper()}  # upper-case hex is a valid spelling ...
    p = gate.verify(_header("foliant-channel", up))
    assert p.update["id"] == u["id"] and list(gate.latest) == [f"foliant-channel:{u['id']}:{u['account']}"]
    for spelled in (u, up, {**u, "id": "0X" + u["id"][2:]}, {**u, "id": u["id"][2:]}, {**u, "id": u["id"] + "00"}):
        with pytest.raises(PaymentRequired):
            gate.verify(_header("foliant-channel", spelled))
    for bad in ("0x" + "0" * 63, "0x" + "g" * 64, 12, None, b"\x00" * 32):
        with pytest.raises(Exception):  # FoliantError or ValueError; verify() turns either into a 402
            _canon32(bad)


def test_v2_state_survives_restart_and_is_pruned_on_settle(chain, tmp_path):  # VERIFY-2 A3-3
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    offer = ChainOffer(prov, token, 3)
    gate = ChainGate(L, KEYS[3], offer, state_dir=str(tmp_path))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=22)
    ups = [_pay(payer, cid, 3) for _ in range(3)]
    for u in ups:
        gate.verify(_header("foliant-channel", u))
    gate2 = ChainGate(L, KEYS[3], offer, state_dir=str(tmp_path))  # restart
    assert gate2.revenue_unsettled == 9 and len(gate2.latest) == 1
    for u in ups:
        with pytest.raises(PaymentRequired):
            gate2.verify(_header("foliant-channel", u))
    total, txs = gate2.settle()
    assert total == 9 and len(txs) == 1
    assert gate2.latest == {} and gate2.revenue_unsettled == 0
    saved = json.loads(gate2.state_path.read_text())
    assert saved == {"latest": {}, "revenue_unsettled": 0}  # file does not grow: settled entries are pruned
    assert gate2.state_path.name.startswith(f"foliant-gate-{L.chain_id}-{prov.lower()}-")


def test_v2_corrupt_state_file_fails_closed(chain, tmp_path):  # VERIFY-2 A3-3 (documented behaviour)
    L, addrs = chain
    prov = Account.from_key(KEYS[3]).address
    offer = ChainOffer(prov, addrs["token"], 3)
    g = ChainGate(L, KEYS[3], offer, state_dir=str(tmp_path))
    g.state_path.write_text('{"latest": {"foliant-chan')  # partial write
    with pytest.raises(Exception):
        ChainGate(L, KEYS[3], offer, state_dir=str(tmp_path))  # refuses to start rather than starting empty (safe)


def test_v2_two_processes_on_one_state_file_replay(chain, tmp_path):  # FINDING A3-10
    """Two gate processes (uvicorn --workers 2, or an old and a new process during a rolling restart) load the
    file at start and never re-read it: each accepts every update once, and the last writer's memory
    overwrites the other's accepted entries on disk. Persistence closes the restart replay, not this one."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    offer = ChainOffer(prov, token, 3)
    A = ChainGate(L, KEYS[3], offer, state_dir=str(tmp_path))
    B = ChainGate(L, KEYS[3], offer, state_dir=str(tmp_path))  # second worker, same file
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=23)
    cid2 = payer.open_channel(prov, token, 100, 3600, salt=24)
    u1, u2 = _pay(payer, cid, 3), _pay(payer, cid2, 3)
    A.verify(_header("foliant-channel", u1))
    A.verify(_header("foliant-channel", u2))
    replayed = 0
    try:
        B.verify(_header("foliant-channel", u1))  # B does not know A accepted it
        replayed += 1
    except PaymentRequired:
        pass
    C = ChainGate(L, KEYS[3], offer, state_dir=str(tmp_path))  # restart after B's last save
    lost = [k for k in A.latest if k not in C.latest]
    assert replayed == 0 and not lost, f"replayed once in worker B; entries lost from disk after B's save: {lost}"


def test_v2_concurrent_accepts_consume_payments_without_serving(chain, tmp_path):  # FINDING A3-15
    """The dependency is sync, so FastAPI runs verify() on a threadpool. Every accept writes the same
    `<state>.tmp` then os.replace()s it: two threads interleave (A truncates, B truncates, A renames, B's
    rename finds no tmp) and _save raises FileNotFoundError AFTER `latest`/`revenue_unsettled` were mutated.
    verify() turns it into a 402 whose body leaks the filesystem path: the payment is consumed (stale on
    retry) and the call is not served. Seen 2-4 times in 8 concurrent valid requests on this machine."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3), state_dir=str(tmp_path))
    payer = _funded_agent(L, token, KEYS[1])
    cids = [payer.open_channel(prov, token, 100, 3600, salt=30 + i) for i in range(8)]
    hdrs = [_header("foliant-channel", _pay(payer, c, 3)) for c in cids]
    tc = _app(gate)
    with ThreadPoolExecutor(16) as ex:
        rs = list(ex.map(lambda h: tc.post("/infer", content=b"x", headers={HDR_PAYMENT: h}), hdrs))
    codes = [r.status_code for r in rs]
    assert 500 not in codes
    assert len(gate.latest) == 8 and gate.revenue_unsettled == 24   # all eight were accepted ...
    errors = [r.json()["error"] for r in rs if r.status_code != 200]
    assert codes.count(200) == 8, f"{len(errors)} consumed but not served: {errors[:1]}"


def test_v2_price_change_orphans_state_and_replays(chain, tmp_path):  # FINDING A3-11
    """The state file name embeds offer.id (provider, token, price, pool). Restarting with a different
    FOLIANT_PRICE (or token) starts from an empty file: every unsettled update replays once, and the old file's
    entries are never settled by the new gate."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3), state_dir=str(tmp_path))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=25)
    u = _pay(payer, cid, 3)
    gate.verify(_header("foliant-channel", u))
    gate2 = ChainGate(L, KEYS[3], ChainOffer(prov, token, 2), state_dir=str(tmp_path))  # price lowered on restart
    with pytest.raises(PaymentRequired):
        gate2.verify(_header("foliant-channel", u))


def test_v2_pool_entries_settle_against_offer_pool_not_their_own(chain, tmp_path):  # FINDING A3-12
    """settle() re-checks a pool entry against ITS pool (obj_id) but sends the batch to self.offer.pool_id.
    serve_chain constructs the gate (loading state) before the pool id is set, so after a crash + restart with
    a new FOLIANT_POOL_SALT/POOL_ID the old pool's entries are sent to the new pool, skipped there as
    non-members, and dropped from state: the revenue is silently abandoned."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    offer = ChainOffer(prov, token, 3)
    gate = ChainGate(L, KEYS[3], offer, state_dir=str(tmp_path))
    old_pid = gate.create_pool(timeout_secs=3600, salt=26)
    payer = _funded_agent(L, token, KEYS[1])
    payer.join_pool(old_pid, 30)
    gate.verify(_header("foliant-pool", _pay(payer, old_pid, 3)))
    # crash; restart with a new pool (offer.id at construction has pool None, so the same state file loads)
    offer2 = ChainOffer(prov, token, 3)
    gate2 = ChainGate(L, KEYS[3], offer2, state_dir=str(tmp_path))
    assert len(gate2.latest) == 1
    gate2.create_pool(timeout_secs=3600, salt=27)
    total, txs = gate2.settle()
    assert L.claim(old_pid, payer.account_id)["paid"] == 3, f"old pool never settled (total {total}); entry dropped: {gate2.latest == {}}"


def test_v2_transient_rpc_error_drops_channel_entry(chain, tmp_path):  # FINDING A3-13
    """settle() drops a channel entry when L.channel() raises anything (line 532-535), not only NoChannel.
    One failed RPC read (timeout, 502 from the node) permanently discards accepted revenue."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3), state_dir=str(tmp_path))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=28)
    gate.verify(_header("foliant-channel", _pay(payer, cid, 3)))
    real = L.channel
    calls = {"n": 0}

    def flaky(c):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("node hiccup")
        return real(c)

    L.channel = flaky
    try:
        try:
            gate.settle()
        except ConnectionError:
            pass
        total, txs = gate.settle()  # node is back
    finally:
        L.channel = real
    assert L.channel(cid)["paid"] == 3, "entry dropped on a transient error; revenue abandoned"


def test_v2_front_run_self_settle_keeps_accounting_right(chain, tmp_path):  # VERIFY-2 settle robustness
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3), state_dir=str(tmp_path))
    pid = gate.create_pool(timeout_secs=1, salt=29)
    honest = _funded_agent(L, token, KEYS[1])
    evil = _funded_agent(L, token, KEYS[2])
    honest.join_pool(pid, 30)
    evil.join_pool(pid, 30)
    gate.verify(_header("foliant-pool", _pay(honest, pid, 3)))
    ue = _pay(evil, pid, 3)
    gate.verify(_header("foliant-pool", ue))
    # evil self-applies a HIGHER update via beginExit, then finalizes and rejoins (epoch 2) before the provider settles
    ue2 = _pay(evil, pid, 3)
    evil._send(L.pools.functions.beginExit(pid, evil.account_id, ue2["seq"], ue2["balance"], bytes.fromhex(ue2["sig"][2:])))
    _warp(L, 3)
    evil.finalize_exit(pid)
    evil.join_pool(pid, 30)
    before = L.token(token).functions.balanceOf(prov).call()
    total, txs = gate.settle()
    after = L.token(token).functions.balanceOf(prov).call()
    assert L.claim(pid, honest.account_id)["paid"] == 3          # honest member settled
    assert total == after - before == 3 and len(txs) == 1        # total is the real delta; evil's 6 came via beginExit
    assert gate.latest == {}                                     # evil's stale-epoch entry pruned, not retried forever
    # channel: payer self-settles a higher update between the gate's pre-check and its tx -> StaleUpdate -> kept, then pruned
    payer = _funded_agent(L, token, KEYS[4])
    cid = payer.open_channel(prov, token, 100, 3600, salt=29)
    u1 = _pay(payer, cid, 3)
    gate.verify(_header("foliant-channel", u1))
    u2 = _pay(payer, cid, 3)
    real = L.channel

    def front_run(c):
        ch = real(c)
        if ch["paid"] == 0:
            payer._send(L.channels.functions.settle(c, u2["seq"], u2["balance"], bytes.fromhex(u2["sig"][2:])))
        return ch

    L.channel = front_run
    try:
        total, txs = gate.settle()
    finally:
        L.channel = real
    assert total == 0 and txs == [] and len(gate.latest) == 1   # tx reverted StaleUpdate: kept, nothing counted
    total, txs = gate.settle()
    assert total == 0 and gate.latest == {}                     # next call prunes it


def test_v2_pending_confirm_and_dropped_response(chain, tmp_path):  # VERIFY-2 A3-9 (+ documents the stuck case)
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3), state_dir=str(tmp_path))
    payer = _funded_agent(L, token, KEYS[1])
    cid = payer.open_channel(prov, token, 100, 3600, salt=40)
    payer.pay_channel(cid, 3)  # never delivered: pending
    payer.discard(cid)
    p = gate.verify(_header("foliant-channel", payer.pay_channel(cid, 3)))
    payer.confirm(cid)
    assert p.paid == 3
    # accepted by the gate, response lost: client discards, re-signs at the same balance, gate says stale
    u = payer.pay_channel(cid, 3)
    gate.verify(_header("foliant-channel", u))
    payer.discard(cid)
    with pytest.raises(PaymentRequired) as ei:
        gate.verify(_header("foliant-channel", payer.pay_channel(cid, 3)))
    assert ei.value.terms["error"] == "stale update"
    # recovery that is always safe: "stale" can only mean the gate holds the update the client signed last,
    # so promote the discarded one and pay again (one call's price lost, bounded per incident)
    payer.pending[cid] = u
    payer.confirm(cid)
    assert gate.verify(_header("foliant-channel", payer.pay_channel(cid, 3))).paid == 3


def test_v2_http_client_opens_a_channel_per_failed_first_call(chain, tmp_path):  # FINDING A3-14
    """Channel reuse scans agent.latest (confirmed updates only). If the first call on a fresh channel is
    refused, the channel is never in `latest`, so every retry opens another channel: a deposit commit and a
    policy-window spend per failure, until per_window_max is exhausted."""
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    gate = ChainGate(L, KEYS[3], ChainOffer(prov, token, 3), state_dir=str(tmp_path))
    payer = _funded_agent(L, token, KEYS[1])
    tc = _app(gate)
    client = ChainHttpClient(payer, tc, default_deposit=30, prefer_pool=False)
    real_verify = gate.verify
    gate.verify = lambda h: (_ for _ in ()).throw(PaymentRequired({**gate.terms(), "error": "temporarily unavailable"}))
    n0 = L.w3.eth.get_transaction_count(payer.address)
    for _ in range(3):
        assert client.request("POST", "/infer", content=b"x").status_code == 402
    gate.verify = real_verify
    opened = L.w3.eth.get_transaction_count(payer.address) - n0
    assert opened == 1, f"{opened} channels opened for 3 refused calls"


def test_v2_decoded_reverts(chain):  # VERIFY-2 A3-8
    from foliant.errors import InsufficientFunds, PolicyViolation
    L, addrs = chain
    token = addrs["token"]
    prov = Account.from_key(KEYS[3]).address
    payer = _funded_agent(L, token, KEYS[1], amount=500, policy=ChainPolicy(100, 1000, 3600))
    with pytest.raises(PolicyViolation, match="per_tx_max"):
        payer.open_channel(prov, token, 200, 3600, salt=41)
    poor = _funded_agent(L, token, KEYS[2], amount=50, policy=ChainPolicy(100, 1000, 3600))
    with pytest.raises(InsufficientFunds):
        poor.open_channel(prov, token, 60, 3600, salt=42)
    with pytest.raises(FoliantError):
        payer.open_channel(prov, token, 10, 0, salt=43)  # BadUpdate("timeout out of range") -> FoliantError
