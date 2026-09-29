"""Ledger node: the in-memory ledger behind HTTP, so clients in other languages can use it.

Devnet only. Mount `ledger_router(ledger)` on any FastAPI app; the metered demo
API mounts it beside the paid endpoint so one process is both the chain and
the provider. Every value-moving call is an account-signed envelope applied
through `Ledger.apply`, exactly as in-process callers do; the node adds no
authority of its own. `/faucet` mints and exists only because this is a devnet.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .accounts import Attestation, Policy
from .crypto import PublicKey, Signed
from .errors import FoliantError
from .ledger import Ledger


class RegisterBody(BaseModel):
    owner_signed: dict
    signer: dict
    policy: dict
    attestation: Optional[dict] = None
    salt: int = 0


class TxBody(BaseModel):
    envelope: dict
    escalation: Optional[dict] = None


class FaucetBody(BaseModel):
    address: str
    asset: str
    amount: int


def _account_view(L: Ledger, acct_id: str) -> dict:
    a = L.accounts.get(acct_id)
    if a is None:
        raise HTTPException(404, f"no account {acct_id}")
    return {
        "id": a.id, "address": a.address, "owner": a.owner.to_dict(), "signer": a.signer.to_dict(),
        "policy": a.policy.to_dict(), "nonce": a.nonce, "parent": a.parent,
        "spent_in_window": a.window.spent(L.now, a.policy.window_secs),
        "balances": dict(L.balances.get(a.address, {})),
    }


def _channel_view(L: Ledger, cid: str) -> dict:
    c = L.channels.get(cid)
    if c is None:
        raise HTTPException(404, f"no channel {cid}")
    return {"id": c.id, "payer_account": c.payer_account, "payee": c.payee, "asset": c.asset, "deposit": c.deposit,
            "balance_to_payee": c.balance_to_payee, "seq": c.seq, "timeout_secs": c.timeout_secs,
            "closing_at": c.closing_at, "closed": c.closed}


def _pool_view(L: Ledger, pid: str) -> dict:
    p = L.pools.get(pid)
    if p is None:
        raise HTTPException(404, f"no pool {pid}")
    return {"id": p.id, "coordinator": p.coordinator, "asset": p.asset, "timeout_secs": p.timeout_secs,
            "members": {mid: {"deposit": m.deposit, "paid": m.paid, "seq": m.seq, "exit_at": m.exit_at, "exited": m.exited}
                        for mid, m in p.members.items()}}


def ledger_router(L: Ledger, *, faucet: bool = True) -> APIRouter:
    r = APIRouter(prefix="/ledger")

    @r.get("/now")
    def now() -> dict:
        return {"now": L.now}

    @r.post("/accounts")
    def register(b: RegisterBody) -> dict:
        try:
            acct = L.register_account(
                Signed.from_dict(b.owner_signed), signer=PublicKey.from_dict(b.signer), policy=Policy.from_dict(b.policy),
                attestation=Attestation(**b.attestation) if b.attestation else None, salt=b.salt)
        except FoliantError as e:
            raise HTTPException(400, str(e))
        return _account_view(L, acct.id)

    @r.get("/accounts/{acct_id}")
    def account(acct_id: str) -> dict:
        return _account_view(L, acct_id)

    @r.post("/tx")
    def tx(b: TxBody) -> dict:
        try:
            result = L.apply(Signed.from_dict(b.envelope), Signed.from_dict(b.escalation) if b.escalation else None)
        except FoliantError as e:
            raise HTTPException(400, f"{type(e).__name__}: {e}")
        return {"result": result}

    @r.get("/channels/{cid}")
    def channel(cid: str) -> dict:
        return _channel_view(L, cid)

    @r.get("/pools/{pid}")
    def pool(pid: str) -> dict:
        return _pool_view(L, pid)

    @r.get("/balances/{address}/{asset}")
    def balance(address: str, asset: str) -> dict:
        return {"address": address, "asset": asset, "balance": L.balance(address, asset)}

    if faucet:
        @r.post("/faucet")
        def mint(b: FaucetBody) -> dict:
            if b.amount <= 0 or b.amount > 1_000_000:
                raise HTTPException(400, "amount must be 1..1000000")
            L.mint(b.address, b.asset, b.amount)
            return {"balance": L.balance(b.address, b.asset)}

    return r
