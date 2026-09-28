"""A metered API: one endpoint that charges per call through the Foliant 402 gate."""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request, Response

from foliant import KeyPair, Ledger, ServiceOffer
from foliant.x402 import Payment, PaymentGate, install


def build_app(ledger: Ledger, provider: KeyPair, offer: ServiceOffer) -> tuple[FastAPI, PaymentGate]:
    gate = PaymentGate(ledger, offer, provider)
    app = FastAPI(title="Foliant metered API")
    install(app)

    @app.post("/infer")
    async def infer(request: Request, payment: Payment = Depends(gate.dependency())):
        body = await request.body()
        # stand-in for a model call
        answer = {"echo": body.decode(), "paid": payment.amount, "scheme": payment.scheme}
        out = Response(content=__import__("json").dumps(answer), media_type="application/json")
        out.headers["X-PAYMENT-RESPONSE"] = gate.receipt(payment, body, out.body)
        return out

    @app.get("/health")
    def health():
        return {"ok": True, "unsettled": gate.revenue_unsettled}

    return app, gate
