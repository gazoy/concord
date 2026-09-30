"""The EVM backend against a local Anvil chain: the crew scenario, the x402 flow, and settlement."""
import os
import shutil
import socket
import subprocess
import time

import httpx
import pytest

pytest.importorskip("web3")
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from web3 import Web3

from foliant.chain import (HDR_PAYMENT, HDR_RECEIPT, ChainAgent, ChainGate, ChainHttpClient, ChainLedger,
                           ChainOffer, ChainPayment, ChainPolicy, PaymentRequired, deploy_local)
from foliant.errors import PolicyViolation

# anvil's default funded keys
KEYS = ["0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
        "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
        "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
        "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"]

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
    from eth_account import Account
    a = Account.from_key(key)
    t = L.token(token)
    tx = t.functions.mint(to, amount).build_transaction({"from": a.address, "nonce": L.w3.eth.get_transaction_count(a.address),
                                                          "chainId": L.chain_id})
    L.w3.eth.wait_for_transaction_receipt(L.w3.eth.send_raw_transaction(a.sign_transaction(tx).raw_transaction))


def test_crew_scenario_on_chain(chain):
    L, addrs = chain
    token = addrs["token"]
    orch = ChainAgent(L, KEYS[1])
    worker = ChainAgent(L, KEYS[2])
    provider = ChainAgent(L, KEYS[3])
    root = orch.register(ChainPolicy(500, 2000, 3600), salt=1)
    _mint(L, token, orch.address, 10_000)
    orch.deposit(token, 10_000)
    # delegate a worker with a tighter policy; funding is not a spend
    w = orch.delegate(worker.address, ChainPolicy(100, 300, 3600), token, 1000, salt=1)
    worker.account_id = w
    assert L.balance(w, token) == 1000 and L.account(root)["spent_in_window"] == 0
    # worker opens a channel at its per-tx cap and pays three calls of 3
    cid = worker.open_channel(provider.address, token, 100, 3600, salt=1)
    for _ in range(3):
        u = worker.pay_channel(cid, 3)
        worker.confirm(cid)  # the provider accepted it
    assert u["balance"] == 9
    # the worker's commitment is counted up the tree
    assert L.account(w)["spent_in_window"] == 100 and L.account(root)["spent_in_window"] == 100
    # a fourth channel would exceed the worker's window cap of 300 (100 each): three more openings and the fourth fails
    from foliant.errors import PolicyViolation as PV
    with pytest.raises(PV):
        for i in range(2, 6):
            worker.open_channel(provider.address, token, 100, 3600, salt=i)
    # provider settles the session in one transaction; orchestrator recalls the rest
    gate = ChainGate(L, KEYS[3], ChainOffer(provider.address, token, 3))
    gate.latest[f"foliant-channel:{u['id']}:{u['account']}"] = u
    total, txs = gate.settle()
    assert total == 9 and len(txs) == 1
    assert L.token(token).functions.balanceOf(provider.address).call() == 9
    orch.recall(w, token)
    assert L.balance(w, token) == 0


def test_x402_flow_over_chain(chain):
    L, addrs = chain
    token = addrs["token"]
    provider_key = KEYS[3]
    from eth_account import Account
    prov = Account.from_key(provider_key).address
    offer = ChainOffer(prov, token, 3)
    gate = ChainGate(L, provider_key, offer)
    pid = gate.create_pool(timeout_secs=60, salt=7)

    app = FastAPI()

    @app.exception_handler(PaymentRequired)
    async def _h(_r, exc):
        return JSONResponse(status_code=402, content=exc.terms)

    @app.post("/infer")
    async def infer(request: Request, payment: ChainPayment = Depends(gate.dependency())):
        body = await request.body()
        out = b'{"echo":' + body + b"}"
        return Response(out, media_type="application/json", headers={HDR_RECEIPT: gate.receipt(payment, body, out)})

    tc = TestClient(app)
    agent = ChainAgent(L, KEYS[1])
    agent.register(ChainPolicy(500, 2000, 3600), salt=2)
    _mint(L, token, agent.address, 1000)
    agent.deposit(token, 1000)
    client = ChainHttpClient(agent, tc, default_deposit=30)

    # first call joins the pool (a 30 deposit, one policy check) and pays 3; next nine are off-chain only
    txs_before = L.w3.eth.get_transaction_count(agent.address)
    r = client.request("POST", "/infer", content=b'"hi"')
    assert r.status_code == 200, r.text
    assert r.json() == {"echo": "hi"}
    for _ in range(9):
        assert client.request("POST", "/infer", content=b'"x"').status_code == 200
    assert L.w3.eth.get_transaction_count(agent.address) - txs_before == 1  # the join; ten calls, one transaction
    assert len(client.receipts) == 10 and client.receipts[-1]["signer"]["key"] == prov
    # the eleventh needs a fresh deposit; the pool claim is exhausted
    with pytest.raises(PolicyViolation):
        agent.pay_pool(pid, 3)
    # a replayed or forged header is refused with 402
    replay = tc.post("/infer", content=b'"y"', headers={HDR_PAYMENT: tc.headers.get(HDR_PAYMENT, "")})
    assert replay.status_code == 402
    # provider settles the whole session in one transaction
    total, txs = gate.settle()
    assert total == 30 and len(txs) == 1
    assert L.claim(pid, agent.account_id)["paid"] == 30
    # member exits (deposit fully spent: nothing comes back) and the gate refuses further payments from it
    agent.begin_exit(pid)
    assert L.claim(pid, agent.account_id)["exit_at"]
