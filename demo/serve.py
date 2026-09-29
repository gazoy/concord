"""Run the devnet ledger node and the metered demo API on one port, for clients in other languages.

    python demo/serve.py            # http://127.0.0.1:8402

Endpoints: POST /infer (x402, 3 units per call); /ledger/* (see foliant/node.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from foliant import KeyPair, Ledger, ServiceOffer
from demo.api import build_app

ASSET = "USDC"


def make_app():
    L = Ledger()
    provider = KeyPair.from_seed(b"provider")
    L.mint(provider.address, ASSET, 1_000)
    pool = L.create_pool(provider.address, ASSET, timeout_secs=60, bond=1_000)
    offer = ServiceOffer(provider=provider.address, asset=ASSET, price_per_unit=3, unit="call",
                         descriptor={"model": "echo-1"}, pool_id=pool.id)
    L.publish_offer(offer)
    app, gate = build_app(L, provider, offer)

    @app.post("/settle")
    def settle():
        """Provider settles everything outstanding (demo convenience; the provider would do this on a schedule)."""
        return {"settled": gate.settle(), "provider_balance": L.balance(provider.address, ASSET)}

    return app


if __name__ == "__main__":
    uvicorn.run(make_app(), host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8402)
