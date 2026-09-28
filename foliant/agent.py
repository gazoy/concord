"""Agent-side convenience: builds envelopes and off-chain updates for one account.

This is what an agent SDK wraps. The signer runs the same policy as the ledger,
so a payment the enclave refuses to sign is one the chain would have rejected.
"""
from __future__ import annotations

from typing import Optional

from .accounts import AgentSigner, Attestation, Policy
from .crypto import KeyPair, Signed, sign
from .ledger import Ledger


class Agent:
    def __init__(self, ledger: Ledger, owner: KeyPair, signer_kp: KeyPair, policy: Policy,
                 attestation: Optional[Attestation] = None, salt: int = 0):
        self.ledger = ledger
        self.owner = owner
        reg = sign(owner, {"op": "register", "signer": signer_kp.public.to_dict(), "policy_id": policy.id, "salt": salt})
        self.account = ledger.register_account(reg, signer=signer_kp.public, policy=policy, attestation=attestation, salt=salt)
        self.signer = AgentSigner(signer_kp, policy, self.account.id)
        self._channel_seq: dict[str, int] = {}
        self._pool_seq: dict[str, int] = {}
        self.latest: dict[str, Signed] = {}  # obj id -> last update we signed

    # --- envelopes (on-chain) --------------------------------------------

    def _env(self, op: str, **params) -> Signed:
        body = {"account": self.account.id, "nonce": self.account.nonce, "op": op, **params}
        return self.signer.sign_plain(body)

    def submit(self, op: str, escalate_with: Optional[KeyPair] = None, *,
               spend: int = 0, payee_addr: str = "", **params) -> dict:
        """Sign and apply an envelope. Value-moving ops pass `spend`/`payee_addr` so the
        signer runs the same policy check the ledger will run."""
        body = {"account": self.account.id, "nonce": self.account.nonce, "op": op, **params}
        if spend or payee_addr:
            env = self.signer.sign_payment(payee=payee_addr, amount=spend, now=self.ledger.now, body=body,
                                           escalated=escalate_with is not None)
        else:
            env = self.signer.sign_plain(body)
        esc = sign(escalate_with, env.body) if escalate_with else None
        return self.ledger.apply(env, esc)

    def transfer(self, to: str, asset: str, amount: int, **kw) -> dict:
        return self.submit("transfer", spend=amount, payee_addr=to, to=to, asset=asset, amount=amount, **kw)

    def open_channel(self, payee: str, asset: str, deposit: int, timeout_secs: int = 3600, salt: int = 0, **kw) -> str:
        return self.submit("open_channel", spend=deposit, payee_addr=payee, payee=payee, asset=asset, deposit=deposit,
                           timeout_secs=timeout_secs, salt=salt, **kw)["channel_id"]

    def close_channel(self, channel_id: str) -> dict:
        latest = self.latest.get(channel_id)
        return self.submit("close_channel", channel_id=channel_id, latest=latest.to_dict() if latest else None)

    def finalize_close(self, channel_id: str) -> dict:
        return self.submit("finalize_close", channel_id=channel_id)

    def join_pool(self, pool_id: str, deposit: int, **kw) -> dict:
        coordinator = self.ledger.pools[pool_id].coordinator
        return self.submit("join_pool", spend=deposit, payee_addr=coordinator, pool_id=pool_id, deposit=deposit, **kw)

    def begin_exit(self, pool_id: str) -> dict:
        latest = self.latest.get(pool_id)
        return self.submit("begin_exit", pool_id=pool_id, latest=latest.to_dict() if latest else None)

    def finalize_exit(self, pool_id: str) -> dict:
        return self.submit("finalize_exit", pool_id=pool_id)

    # --- off-chain updates -----------------------------------------------

    def pay_channel(self, channel_id: str, amount: int) -> Signed:
        """Sign the next channel update adding `amount` for the payee."""
        ch = self.ledger.channels[channel_id]
        seq = self._channel_seq.get(channel_id, ch.seq) + 1
        balance = (self.latest[channel_id].body["balance"] if channel_id in self.latest else ch.balance_to_payee) + amount
        if balance > ch.deposit:
            raise ValueError("channel deposit exhausted")
        u = self.signer.sign_update(kind="channel", obj_id=channel_id, payee=ch.payee, seq=seq, balance=balance, now=self.ledger.now)
        self._channel_seq[channel_id] = seq
        self.latest[channel_id] = u
        return u

    def pay_pool(self, pool_id: str, amount: int) -> Signed:
        pool = self.ledger.pools[pool_id]
        claim = pool.members[self.account.id]
        seq = self._pool_seq.get(pool_id, claim.seq) + 1
        balance = (self.latest[pool_id].body["balance"] if pool_id in self.latest else claim.paid) + amount
        if balance > claim.deposit:
            raise ValueError("pool deposit exhausted")
        u = self.signer.sign_update(kind="pool", obj_id=pool_id, payee=pool.coordinator, seq=seq, balance=balance, now=self.ledger.now)
        self._pool_seq[pool_id] = seq
        self.latest[pool_id] = u
        return u

    def start_stream(self, channel_id: str, rate_per_sec: int) -> Signed:
        ch = self.ledger.channels[channel_id]
        # the deposit was authorised at open; a stream only schedules it
        body = {"kind": "stream", "id": channel_id, "account": self.account.id, "rate": rate_per_sec, "start": self.ledger.now}
        s = self.signer.sign_payment(payee=ch.payee, amount=0, now=self.ledger.now, body=body)
        self.ledger.start_stream(channel_id, s)
        return s
