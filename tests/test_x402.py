"""HTTP 402 gate: terms, verification, underpayment, attestation requirement, settlement."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from foliant import Attestation, KeyPair, Ledger, Policy, ServiceOffer
from foliant.x402 import HDR_PAYMENT, HDR_RECEIPT, AgentHttpClient
from demo.api import build_app
from tests.conftest import ASSET, make_agent


def setup(required_code_hash=None, with_pool=False):
    L = Ledger()
    provider = KeyPair.from_seed(b"prov")
    L.mint(provider.address, ASSET, 100)
    pool = L.create_pool(provider.address, ASSET, timeout_secs=10, bond=100) if with_pool else None
    offer = ServiceOffer(provider=provider.address, asset=ASSET, price_per_unit=5, unit="call",
                         required_code_hash=required_code_hash, pool_id=pool.id if pool else None)
    app, gate = build_app(L, provider, offer)
    return L, provider, offer, gate, TestClient(app)


def test_unpaid_request_gets_x402_terms():
    L, provider, offer, gate, http = setup()
    r = http.post("/infer", content="hi")
    assert r.status_code == 402
    body = r.json()
    assert body["x402Version"] == 1
    assert body["accepts"][0]["scheme"] == "foliant-channel"
    assert body["accepts"][0]["payTo"] == provider.address


def test_paid_request_served_and_receipt_returned():
    L, provider, offer, gate, http = setup()
    a = make_agent(L, "a")
    c = AgentHttpClient(a, http, default_deposit=50, prefer_pool=False)
    r = c.post("/infer", content="hello")
    assert r.status_code == 200 and r.json()["paid"] == 5
    receipt = json.loads(base64.b64decode(r.headers[HDR_RECEIPT]))
    assert receipt["body"]["offerId"] == offer.id
    assert gate.revenue_unsettled == 5
    assert gate.settle() == 5
    assert L.balance(provider.address, ASSET) == 105


def test_replayed_payment_rejected():
    L, provider, offer, gate, http = setup()
    a = make_agent(L, "a")
    c = AgentHttpClient(a, http, default_deposit=50, prefer_pool=False)
    c.post("/infer", content="1")
    cid = next(iter(L.channels))
    replay = base64.b64encode(json.dumps({"scheme": "foliant-channel", "id": cid,
                                          "update": a.latest[cid].to_dict()}).encode()).decode()
    r = http.post("/infer", content="2", headers={HDR_PAYMENT: replay})
    assert r.status_code == 402 and "stale" in r.json()["error"]


def test_underpayment_rejected():
    L, provider, offer, gate, http = setup()
    a = make_agent(L, "a")
    cid = a.open_channel(provider.address, ASSET, 50)
    u = a.pay_channel(cid, 4)  # price is 5
    hdr = base64.b64encode(json.dumps({"scheme": "foliant-channel", "id": cid, "update": u.to_dict()}).encode()).decode()
    r = http.post("/infer", content="x", headers={HDR_PAYMENT: hdr})
    assert r.status_code == 402 and "underpaid" in r.json()["error"]


def test_attestation_gate():
    L, provider, offer, gate, http = setup(required_code_hash="agent-v1")
    vendor = KeyPair.from_seed(b"vendor")
    L.trusted_vendors.add(vendor.public.hex)
    plain = make_agent(L, "plain")
    c = AgentHttpClient(plain, http, default_deposit=50, prefer_pool=False)
    assert c.post("/infer", content="x").status_code == 402
    from foliant import Agent
    skp = KeyPair.from_seed(b"attested-s")
    att = Agent(L, KeyPair.from_seed(b"attested-o"), skp, Policy(1000, 5000, 60),
                attestation=Attestation.issue(vendor, "agent-v1", skp.public))
    L.mint(att.account.address, ASSET, 1000)
    c2 = AgentHttpClient(att, http, default_deposit=50, prefer_pool=False)
    assert c2.post("/infer", content="x").status_code == 200


def test_pool_scheme_end_to_end():
    L, provider, offer, gate, http = setup(with_pool=True)
    agents = [make_agent(L, f"a{i}") for i in range(3)]
    clients = [AgentHttpClient(a, http, default_deposit=50, prefer_pool=True) for a in agents]
    for _ in range(4):
        for c in clients:
            assert c.post("/infer", content="x").status_code == 200
    assert gate.settle() == 3 * 4 * 5
    assert L.balance(provider.address, ASSET) == 60  # 100 minted - 100 bond + 60
