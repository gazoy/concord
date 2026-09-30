"""The metered demo API over the Foliant contracts on a live chain (Anvil, Fuji, ...).

    export FOLIANT_RPC=https://api.avax-test.network/ext/bc/C/rpc
    export FOLIANT_ACCOUNTS=0x... FOLIANT_CHANNELS=0x... FOLIANT_POOLS=0x... FOLIANT_TOKEN=0x...
    export PROVIDER_KEY=0x...            # the provider's EVM key (receives settlements)
    python demo/serve_chain.py           # http://127.0.0.1:8402

Endpoints: POST /infer (x402, price per call in FOLIANT_PRICE, default 3 units); GET /chain (addresses,
network, offer); POST /settle (provider settles everything outstanding); POST /tap (testnet funding for a
visitor's address, only when FOLIANT_TAP_KEY is set; per-client limit keyed by the caller's IP, taken from
X-Forwarded-For only when FOLIANT_TRUST_PROXY=1; see demo/tap.py). Agents talk to the chain directly
for opening channels and joining the pool (see foliant.chain.ChainAgent); this server only verifies their
off-chain updates and settles.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from web3 import Web3
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from foliant.chain import ChainGate, ChainLedger, ChainOffer, ChainPayment, PaymentRequired, _hex
from demo.tap import TapError, tap_from_env


def make_app() -> FastAPI:
    env = os.environ
    L = ChainLedger(env["FOLIANT_RPC"], env["FOLIANT_ACCOUNTS"], env["FOLIANT_CHANNELS"], env["FOLIANT_POOLS"])
    from eth_account import Account
    provider = Account.from_key(env["PROVIDER_KEY"]).address
    offer = ChainOffer(provider, env["FOLIANT_TOKEN"], int(env.get("FOLIANT_PRICE", "3")), descriptor={"model": "echo-1"})
    gate = ChainGate(L, env["PROVIDER_KEY"], offer)
    if env.get("FOLIANT_POOL_ID"):
        offer.pool_id = bytes.fromhex(env["FOLIANT_POOL_ID"][2:])
        if L.pool(offer.pool_id)["coordinator"].lower() != provider.lower():
            raise SystemExit("FOLIANT_POOL_ID is not a pool coordinated by PROVIDER_KEY")
    else:
        offer.pool_id = gate.create_pool(timeout_secs=int(env.get("FOLIANT_POOL_TIMEOUT", "3600")),
                                         salt=int(env.get("FOLIANT_POOL_SALT", "0")))
        print("pool created", _hex(offer.pool_id))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        yield
        try:
            gate.settle()  # settle before stopping so nothing accepted is left unsettled (AUDIT-3 A3-3)
        except Exception as e:  # noqa: BLE001
            print("settle on shutdown failed:", e)

    app = FastAPI(title="Foliant metered API (chain)", lifespan=lifespan)
    settle_key = env.get("FOLIANT_SETTLE_KEY")  # if set, POST /settle needs it in X-Settle-Key
    tap = tap_from_env(L, env)
    trust_proxy = env.get("FOLIANT_TRUST_PROXY") == "1"  # behind a reverse proxy that sets X-Forwarded-For
    if tap:
        if tap.address.lower() == provider.lower():
            raise SystemExit("FOLIANT_TAP_KEY must be a wallet used by nothing else, not PROVIDER_KEY (nonce race)")
        print("tap enabled from", tap.address, "balance", Web3.from_wei(tap.balance(), "ether"), "AVAX")

    def tap_client(request: Request) -> str:
        """The caller's IP for the tap's per-client limit (F-1). X-Forwarded-For is client-supplied unless a
        proxy of ours rewrites it, so it is only believed when FOLIANT_TRUST_PROXY=1, and then its last
        entry (the one that proxy appended); otherwise the socket peer."""
        if trust_proxy:
            xff = request.headers.get("x-forwarded-for", "")
            if xff.strip():
                return xff.split(",")[-1].strip()
        return request.client.host if request.client else "unknown"

    @app.exception_handler(PaymentRequired)
    async def _h(_r, exc):
        return JSONResponse(status_code=402, content=exc.terms)

    @app.post("/infer")
    async def infer(request: Request, payment: ChainPayment = Depends(gate.dependency())):
        body = await request.body()
        answer = {"echo": body.decode(), "paid": payment.paid, "scheme": payment.scheme}
        out = Response(content=json.dumps(answer), media_type="application/json")
        out.headers["X-PAYMENT-RESPONSE"] = gate.receipt(payment, body, out.body)
        return out

    @app.get("/chain")
    def chain():
        return {"network": L.network, "chainId": L.chain_id, "contracts": {"accounts": L.accounts.address,
                "channels": L.channels.address, "pools": L.pools.address}, "token": offer.token,
                "provider": provider, "poolId": _hex(offer.pool_id), "price": offer.price_per_unit,
                "tap": tap.status() if tap else None}

    @app.post("/tap")
    async def tap_endpoint(request: Request):
        if tap is None:
            return JSONResponse(status_code=404, content={"error": "no tap on this server"})
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": "JSON body {\"address\": \"0x...\"} required"})
        try:  # give() blocks on the chain for a few seconds: keep it off the event loop (TAP-REVIEW T-4)
            return await run_in_threadpool(tap.give, body.get("address") if isinstance(body, dict) else None,
                                           tap_client(request))
        except TapError as e:
            return JSONResponse(status_code=e.status, content={"error": str(e)})

    @app.get("/health")
    def health():
        return {"ok": True, "unsettled": gate.revenue_unsettled}

    @app.post("/settle")
    def settle(request: Request):
        if settle_key and request.headers.get("X-Settle-Key") != settle_key:
            return JSONResponse(status_code=403, content={"error": "settle key required"})
        total, txs = gate.settle()
        return {"settled": total, "transactions": txs}

    return app


if __name__ == "__main__":
    uvicorn.run(make_app(), host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8402)
