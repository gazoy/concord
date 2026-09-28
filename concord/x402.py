"""HTTP 402 metering (whitepaper §6.5, §6.9).

Wire format follows x402's conventions so an x402-aware agent recognises it:
a 402 response with a JSON body listing accepted payment schemes, a request
header `X-PAYMENT` carrying a base64 JSON payment, and a response header
`X-PAYMENT-RESPONSE` carrying the provider's receipt. Two schemes are defined:

  concord-channel  payment = the payer's next signed channel update
  concord-pool     payment = the payer's next signed pool update

Steps 1-4 of the metering flow touch no chain. `PaymentGate.settle()` is step 5.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Optional

import httpx
from fastapi import Request

from .agent import Agent
from .channels import verify_update
from .crypto import KeyPair, Signed, hash_obj, sha256, sign
from .errors import ConcordError
from .ledger import Ledger
from .market import ServiceOffer

X402_VERSION = 1
HDR_PAYMENT = "X-PAYMENT"
HDR_RECEIPT = "X-PAYMENT-RESPONSE"


def _b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, sort_keys=True).encode()).decode()


def _unb64(s: str) -> dict:
    return json.loads(base64.b64decode(s))


class PaymentRequired(Exception):
    """Raised by the gate; `install(app)` turns it into a bare 402 JSON body (x402 shape)."""

    def __init__(self, terms: dict):
        self.terms = terms


def install(app) -> None:
    from fastapi.responses import JSONResponse

    @app.exception_handler(PaymentRequired)
    async def _handler(_request, exc: PaymentRequired):
        return JSONResponse(status_code=402, content=exc.terms)


@dataclass
class Payment:
    scheme: str
    obj_id: str
    account_id: str
    update: Signed
    amount: int  # what this update added over the last accepted one


@dataclass
class PaymentGate:
    """Provider side. One per offer. Holds the latest accepted update per channel/pool."""

    ledger: Ledger
    offer: ServiceOffer
    provider: KeyPair
    latest: dict[str, Signed] = field(default_factory=dict)  # obj id -> latest accepted update
    revenue_unsettled: int = 0

    def terms(self) -> dict:
        accepts = [{
            "scheme": "concord-channel",
            "network": "concord-devnet",
            "payTo": self.offer.provider,
            "asset": self.offer.asset,
            "maxAmountRequired": str(self.offer.price_per_unit),
            "unit": self.offer.unit,
            "offerId": self.offer.id,
            "requiredCodeHash": self.offer.required_code_hash,
        }]
        if self.offer.pool_id:
            accepts.append({**accepts[0], "scheme": "concord-pool", "poolId": self.offer.pool_id})
        return {"x402Version": X402_VERSION, "accepts": accepts, "error": "payment required"}

    def verify(self, header: Optional[str]) -> Payment:
        """Validate an X-PAYMENT header; raise PaymentRequired with terms if absent or bad."""
        if not header:
            raise PaymentRequired(self.terms())
        try:
            p = _unb64(header)
            scheme, obj_id = p["scheme"], p["id"]
            update = Signed.from_dict(p["update"])
            acct = self.ledger.accounts[update.body["account"]]
            if self.offer.required_code_hash:
                if acct.attestation is None or acct.attestation.code_hash != self.offer.required_code_hash \
                        or not acct.attestation.valid_for(self.ledger.trusted_vendors):
                    raise ConcordError("attestation required")
            if scheme == "concord-channel":
                ch = self.ledger.channels[obj_id]
                if ch.payee != self.offer.provider or ch.closed or ch.asset != self.offer.asset:
                    raise ConcordError("channel not payable to this provider")
                kind, deposit, onchain_seq, onchain_bal = "channel", ch.deposit, ch.seq, ch.balance_to_payee
            elif scheme == "concord-pool":
                pool = self.ledger.pools[obj_id]
                if obj_id != self.offer.pool_id or pool.coordinator != self.offer.provider:
                    raise ConcordError("pool not run by this provider")
                claim = pool.members[acct.id]
                if claim.exited or claim.exit_at is not None:
                    raise ConcordError("member is exiting")
                kind, deposit, onchain_seq, onchain_bal = "pool", claim.deposit, claim.seq, claim.paid
            else:
                raise ConcordError(f"unknown scheme {scheme}")
            seq, balance = verify_update(update, kind=kind, obj_id=obj_id, account_id=acct.id, signer=acct.signer)
            key = f"{kind}:{obj_id}:{acct.id}"
            prev = self.latest.get(key)
            last_seq, last_bal = (prev.body["seq"], prev.body["balance"]) if prev else (onchain_seq, onchain_bal)
            if seq <= last_seq:
                raise ConcordError("stale update")
            if balance > deposit:
                raise ConcordError("update exceeds deposit")
            paid = balance - last_bal
            if paid < self.offer.price_per_unit:
                raise ConcordError(f"underpaid: {paid} < {self.offer.price_per_unit}")
            self.latest[key] = update
            self.revenue_unsettled += paid
            return Payment(scheme, obj_id, acct.id, update, paid)
        except (KeyError, ValueError, ConcordError) as e:
            raise PaymentRequired({**self.terms(), "error": str(e)})

    def dependency(self):
        """FastAPI dependency: `payment: Payment = Depends(gate.dependency())`."""
        def _dep(request: Request) -> Payment:
            return self.verify(request.headers.get(HDR_PAYMENT))
        return _dep

    def receipt(self, payment: Payment, request_body: bytes, response_body: bytes) -> str:
        """Provider-signed receipt for the response header (whitepaper Receipt object)."""
        body = {
            "updateId": hash_obj(payment.update.to_dict()),
            "requestHash": sha256(request_body),
            "responseHash": sha256(response_body),
            "offerId": self.offer.id,
        }
        return _b64(sign(self.provider, body).to_dict())

    def settle(self) -> int:
        """Step 5: submit every latest update on-chain in as few transactions as possible."""
        total = 0
        pool_updates: dict[str, list[Signed]] = {}
        for key, u in list(self.latest.items()):
            kind, obj_id, _ = key.split(":", 2)
            if kind == "channel":
                try:
                    total += self.ledger.payee_settle_channel(obj_id, u)
                except ConcordError:
                    pass  # already at or above this seq
            else:
                pool_updates.setdefault(obj_id, []).append(u)
        for pid, ups in pool_updates.items():
            total += self.ledger.coordinator_settle_pool(pid, ups)
        self.revenue_unsettled = 0
        return total


class AgentHttpClient:
    """Agent side: retries a 402 with a payment, opening a channel or joining a pool on demand."""

    def __init__(self, agent: Agent, http: httpx.Client, *, default_deposit: int = 1000,
                 prefer_pool: bool = True, timeout_secs: int = 3600):
        self.agent, self.http = agent, http
        self.default_deposit, self.prefer_pool, self.timeout_secs = default_deposit, prefer_pool, timeout_secs
        self.receipts: list[dict] = []

    def _choose(self, accepts: list[dict]) -> dict:
        if self.prefer_pool:
            for a in accepts:
                if a["scheme"] == "concord-pool":
                    return a
        return next(a for a in accepts if a["scheme"] == "concord-channel")

    def _payment_for(self, term: dict) -> str:
        L, agent = self.agent.ledger, self.agent
        price = int(term["maxAmountRequired"])
        if term["scheme"] == "concord-pool":
            pid = term["poolId"]
            claim = L.pools[pid].members.get(agent.account.id)
            if claim is None or claim.exited:
                agent.join_pool(pid, self.default_deposit)
            update = agent.pay_pool(pid, price)
            return _b64({"scheme": "concord-pool", "id": pid, "update": update.to_dict()})
        payee, asset = term["payTo"], term["asset"]
        cid = next((c.id for c in L.channels.values()
                    if c.payer_account == agent.account.id and c.payee == payee and c.asset == asset
                    and not c.closed and c.closing_at is None
                    and (agent.latest[c.id].body["balance"] if c.id in agent.latest else c.balance_to_payee) + price <= c.deposit),
                   None)
        if cid is None:
            cid = agent.open_channel(payee, asset, self.default_deposit, timeout_secs=self.timeout_secs,
                                     salt=len(L.channels))
        update = agent.pay_channel(cid, price)
        return _b64({"scheme": "concord-channel", "id": cid, "update": update.to_dict()})

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        r = self.http.request(method, url, **kw)
        if r.status_code != 402:
            return r
        term = self._choose(r.json()["accepts"])
        headers = {**kw.pop("headers", {}), HDR_PAYMENT: self._payment_for(term)}
        r = self.http.request(method, url, headers=headers, **kw)
        if HDR_RECEIPT in r.headers:
            self.receipts.append(_unb64(r.headers[HDR_RECEIPT]))
        return r

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> httpx.Response:
        return self.request("POST", url, **kw)
