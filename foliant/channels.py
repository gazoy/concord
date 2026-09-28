"""Payment channels and streams (whitepaper §6.4, §6.6).

A channel escrows `deposit` from a payer account for one payee. Off-chain the
payer signs updates (channel_id, seq, balance) with balance monotonically
increasing. The payee settles by submitting the latest update at any time;
the payer closes by submitting its latest update and waiting `timeout_secs`
for the payee to submit a higher one.

State machines here are pure: methods validate and return Transfer effects;
the Ledger executes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .crypto import PublicKey, Signed, hash_obj
from .errors import InvalidSignatureError, InvalidUpdate, Unauthorized


@dataclass(frozen=True)
class Transfer:
    src: str  # address, or "escrow:<obj id>"
    dst: str
    asset: str
    amount: int


def verify_update(signed: Signed, *, kind: str, obj_id: str, account_id: str, signer: PublicKey) -> tuple[int, int]:
    """Check an update's signature and shape; return (seq, balance)."""
    if not signed.valid():
        raise InvalidSignatureError("bad signature on update")
    if signed.signer != signer:
        raise Unauthorized("update not signed by the account's current signer")
    b = signed.body
    if b.get("kind") != kind or b.get("id") != obj_id or b.get("account") != account_id:
        raise InvalidUpdate("update is for a different object or account")
    seq, balance = b["seq"], b["balance"]
    if not (isinstance(seq, int) and isinstance(balance, int)) or seq < 0 or balance < 0:
        raise InvalidUpdate("seq and balance must be non-negative integers")
    return seq, balance


@dataclass
class Channel:
    id: str
    payer_account: str
    payee: str  # address
    asset: str
    deposit: int
    timeout_secs: int
    balance_to_payee: int = 0  # settled so far
    seq: int = 0  # seq of the last update applied on-chain
    closing_at: Optional[int] = None
    closed: bool = False
    # streaming: a signed (rate, start) lets the payee claim rate*(now-start)
    stream: Optional[dict] = field(default=None)

    @staticmethod
    def make_id(payer_account: str, payee: str, salt: int) -> str:
        return hash_obj({"payer": payer_account, "payee": payee, "salt": salt})

    @property
    def escrow(self) -> str:
        return f"escrow:{self.id}"

    def _apply_balance(self, seq: int, balance: int) -> list[Transfer]:
        if self.closed:
            raise InvalidUpdate("channel is closed")
        if seq <= self.seq:
            raise InvalidUpdate(f"seq {seq} not greater than last applied {self.seq}")
        if balance < self.balance_to_payee:
            raise InvalidUpdate("balance may not decrease")
        if balance > self.deposit:
            raise InvalidUpdate(f"balance {balance} exceeds deposit {self.deposit}")
        delta = balance - self.balance_to_payee
        self.seq, self.balance_to_payee = seq, balance
        return [Transfer(self.escrow, self.payee, self.asset, delta)] if delta else []

    def settle(self, update: Signed, *, account_id: str, signer: PublicKey) -> list[Transfer]:
        """Payee (or anyone) submits a payer-signed update; pays out the delta."""
        seq, balance = verify_update(update, kind="channel", obj_id=self.id, account_id=account_id, signer=signer)
        return self._apply_balance(seq, balance)

    def begin_close(self, now: int, latest: Optional[Signed], *, account_id: str, signer: PublicKey) -> list[Transfer]:
        """Payer starts closing with its latest update (or none). Payee has `timeout_secs` to top it."""
        if self.closed:
            raise InvalidUpdate("channel is closed")
        effects: list[Transfer] = []
        if latest is not None:
            seq, balance = verify_update(latest, kind="channel", obj_id=self.id, account_id=account_id, signer=signer)
            if seq > self.seq:
                effects = self._apply_balance(seq, balance)
        if self.closing_at is None:
            self.closing_at = now + self.timeout_secs
        return effects

    def finalize_close(self, now: int) -> list[Transfer]:
        """After the timeout, return the unspent deposit to the payer."""
        if self.closed:
            raise InvalidUpdate("channel is closed")
        if self.closing_at is None or now < self.closing_at:
            raise InvalidUpdate("close timeout has not elapsed")
        effects = self.claim_stream(now)
        remaining = self.deposit - self.balance_to_payee
        self.closed = True
        if remaining:
            effects.append(Transfer(self.escrow, f"account:{self.payer_account}", self.asset, remaining))
        return effects

    # --- streaming -------------------------------------------------------

    def start_stream(self, signed: Signed, *, account_id: str, signer: PublicKey) -> None:
        """Payer signs (rate_per_sec, start_ts); claimable grows with time until stopped."""
        if not signed.valid() or signed.signer != signer:
            raise InvalidSignatureError("bad stream signature")
        b = signed.body
        if b.get("kind") != "stream" or b.get("id") != self.id or b.get("account") != account_id:
            raise InvalidUpdate("stream is for a different channel")
        if self.stream is not None:
            raise InvalidUpdate("stream already running")
        self.stream = {"rate": int(b["rate"]), "start": int(b["start"]), "stopped": None}

    def stop_stream(self, now: int) -> None:
        if self.stream and self.stream["stopped"] is None:
            self.stream["stopped"] = now

    def stream_claimable(self, now: int) -> int:
        if not self.stream:
            return 0
        end = self.stream["stopped"] if self.stream["stopped"] is not None else now
        elapsed = max(0, end - self.stream["start"])
        return min(self.deposit, self.balance_to_payee + self.stream["rate"] * elapsed)

    def claim_stream(self, now: int) -> list[Transfer]:
        """Pay out whatever the stream has accrued; folds it into balance_to_payee."""
        if not self.stream:
            return []
        target = self.stream_claimable(now)
        delta = target - self.balance_to_payee
        if delta <= 0:
            return []
        # streams settle by time, not seq; keep seq unchanged
        self.balance_to_payee = target
        if self.stream["stopped"] is not None:
            self.stream = None
        else:
            self.stream["start"] = now  # restart accrual from the claim point
        return [Transfer(self.escrow, self.payee, self.asset, delta)]
