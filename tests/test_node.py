"""The ledger node over HTTP: register, fund, open a channel, pay a 402 endpoint, settle."""
from fastapi.testclient import TestClient

from foliant import KeyPair, Ledger, Policy, ServiceOffer
from foliant.crypto import Signed, sign
from demo.api import build_app

ASSET = "USDC"


def test_node_round_trip_with_raw_envelopes():
    L = Ledger()
    provider = KeyPair.from_seed(b"provider")
    L.mint(provider.address, ASSET, 1_000)
    pool = L.create_pool(provider.address, ASSET, timeout_secs=60, bond=1_000)
    offer = ServiceOffer(provider=provider.address, asset=ASSET, price_per_unit=3, unit="call", descriptor={}, pool_id=pool.id)
    L.publish_offer(offer)
    app, gate = build_app(L, provider, offer)
    http = TestClient(app, base_url="http://node")

    owner, signer = KeyPair.from_seed(b"o"), KeyPair.from_seed(b"s")
    policy = Policy(per_tx_max=500, per_window_max=200, window_secs=3600)
    reg = sign(owner, {"op": "register", "signer": signer.public.to_dict(), "policy_id": policy.id, "salt": 0})
    r = http.post("/ledger/accounts", json={"owner_signed": reg.to_dict(), "signer": signer.public.to_dict(), "policy": policy.to_dict(), "salt": 0})
    assert r.status_code == 200, r.text
    acct = r.json()
    assert http.post("/ledger/faucet", json={"address": acct["address"], "asset": ASSET, "amount": 5_000}).status_code == 200

    # open a channel with a signer-signed envelope, exactly as a remote client would build it
    body = {"account": acct["id"], "nonce": 0, "op": "open_channel", "payee": provider.address, "asset": ASSET,
            "deposit": 100, "timeout_secs": 3600, "salt": 0}
    r = http.post("/ledger/tx", json={"envelope": sign(signer, body).to_dict()})
    assert r.status_code == 200, r.text
    cid = r.json()["result"]["channel_id"]
    assert http.get(f"/ledger/channels/{cid}").json()["deposit"] == 100
    assert http.get(f"/ledger/accounts/{acct['id']}").json()["nonce"] == 1

    # pay the 402 endpoint with a signed channel update, then the provider settles
    import base64, json
    update = sign(signer, {"kind": "channel", "id": cid, "seq": 1, "balance": 3, "account": acct["id"]})
    hdr = base64.b64encode(json.dumps({"scheme": "foliant-channel", "id": cid, "update": update.to_dict()}, sort_keys=True).encode()).decode()
    r = http.post("/infer", content=b"hello", headers={"X-PAYMENT": hdr})
    assert r.status_code == 200 and "X-PAYMENT-RESPONSE" in r.headers
    assert gate.settle() == 3

    # a policy breach is refused with a 400 naming the error
    body = {"account": acct["id"], "nonce": 1, "op": "transfer", "to": KeyPair.from_seed(b"x").address, "asset": ASSET, "amount": 150}
    r = http.post("/ledger/tx", json={"envelope": sign(signer, body).to_dict()})
    assert r.status_code == 400 and "PolicyViolation" in r.text
