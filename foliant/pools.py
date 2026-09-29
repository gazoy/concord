"""Pooled channels (whitepaper §6.10), after Bitcoin's Ark construction.

Many payers fund one pool run by a coordinator (usually the payee). Each payer
holds an individually signed, unilaterally exitable claim. The coordinator
settles the whole pool with one transaction carrying each member's latest
update. If the coordinator disappears, every member's exit still works.

Safety needs no bond: balances are monotonic and payer-signed, so the
coordinator can never collect more than a member signed, and a member that
exits with a stale (low) update is contested by the coordinator within the
window. The coordinator's bond is a liveness bond (settle and contest on
time); its forfeiture is a governance action and is out of scope here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .channels import Transfer, verify_update
from .crypto import PublicKey, Signed, hash_obj
from .errors import InvalidUpdate, NotFound, Unauthorized


@dataclass
class PoolClaim:
    account_id: str
    deposit: int
    paid: int = 0  # settled to coordinator so far
    seq: int = 0
    exit_at: Optional[int] = None
    exited: bool = False
    epoch: int = 1  # increments on each join; signed updates must carry it (AUDIT-2 A2-2)
    signer: Optional[PublicKey] = None  # the member's signer at join: the key whose updates count (A2-4)


@dataclass
class Pool:
    id: str
    coordinator: str  # address that receives settlements
    asset: str
    timeout_secs: int
    bond: int
    members: dict[str, PoolClaim] = field(default_factory=dict)
    slashed: bool = False

    @staticmethod
    def make_id(coordinator: str, salt: int) -> str:
        return hash_obj({"coordinator": coordinator, "salt": salt})

    @property
    def escrow(self) -> str:
        return f"escrow:{self.id}"

    @property
    def bond_escrow(self) -> str:
        return f"bond:{self.id}"

    def join(self, account_id: str, deposit: int, signer: Optional[PublicKey] = None) -> None:
        if account_id in self.members and not self.members[account_id].exited:
            raise InvalidUpdate("already a member")
        if deposit <= 0:
            raise InvalidUpdate("deposit must be positive")
        prev = self.members.get(account_id)
        self.members[account_id] = PoolClaim(account_id, deposit, epoch=(prev.epoch + 1) if prev else 1, signer=signer)

    def _claim(self, account_id: str) -> PoolClaim:
        c = self.members.get(account_id)
        if c is None or c.exited:
            raise NotFound(f"{account_id} is not an active member")
        return c

    def _verify(self, update: Signed, c: PoolClaim, account_id: str, signer: PublicKey) -> tuple[int, int]:
        """verify_update plus the claim's epoch, so an update from an earlier membership cannot replay."""
        seq, balance = verify_update(update, kind="pool", obj_id=self.id, account_id=account_id, signer=c.signer or signer)
        if update.body.get("epoch") != c.epoch:
            raise InvalidUpdate(f"update epoch {update.body.get('epoch')} != claim epoch {c.epoch}")
        return seq, balance

    def _apply(self, c: PoolClaim, seq: int, balance: int) -> list[Transfer]:
        # ordered by balance, not seq (see channels._apply_balance)
        if balance <= c.paid:
            raise InvalidUpdate(f"balance {balance} not greater than settled {c.paid}")
        if balance > c.deposit:
            raise InvalidUpdate("balance out of range")
        delta = balance - c.paid
        c.seq, c.paid = seq, balance
        return [Transfer(self.escrow, self.coordinator, self.asset, delta)] if delta else []

    def settle(self, updates: list[tuple[Signed, str, PublicKey]]) -> list[Transfer]:
        """Coordinator submits [(update, account_id, signer)] for any subset of members.

        Updates that are stale (balance <= settled) or from members who have already
        exited are skipped rather than failing the batch, so one member cannot
        block everyone's settlement.
        """
        effects: list[Transfer] = []
        for update, account_id, signer in updates:
            c = self.members.get(account_id)
            if c is None or c.exited:
                continue
            seq, balance = self._verify(update, c, account_id, signer)
            if balance <= c.paid:
                continue
            effects += self._apply(c, seq, balance)
        return effects

    def begin_exit(self, now: int, account_id: str, latest: Optional[Signed], signer: PublicKey) -> list[Transfer]:
        """Member starts a unilateral exit with its own latest update (or none)."""
        c = self._claim(account_id)
        effects: list[Transfer] = []
        if latest is not None:
            seq, balance = self._verify(latest, c, account_id, signer)
            if balance > c.paid:
                effects = self._apply(c, seq, balance)
        if c.exit_at is None:
            c.exit_at = now + self.timeout_secs
        return effects

    def contest_exit(self, now: int, account_id: str, higher: Signed, signer: PublicKey) -> list[Transfer]:
        """During the exit window anyone may submit a higher update for the exiting member."""
        c = self._claim(account_id)
        if c.exit_at is None or now >= c.exit_at:
            raise InvalidUpdate("no exit window open")
        seq, balance = self._verify(higher, c, account_id, signer)
        return self._apply(c, seq, balance)

    def finalize_exit(self, now: int, account_id: str) -> list[Transfer]:
        c = self._claim(account_id)
        if c.exit_at is None or now < c.exit_at:
            raise InvalidUpdate("exit timeout has not elapsed")
        c.exited = True
        remaining = c.deposit - c.paid
        return [Transfer(self.escrow, f"account:{account_id}", self.asset, remaining)] if remaining else []

