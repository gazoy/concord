"""In-memory ledger: the on-chain side of the Agent chain, for one node.

Everything an agent account does goes through `apply(envelope)`: the envelope
is a Signed body {account, nonce, op, ...}. The ledger checks the signature
against the account's current signer (or owner for maintenance ops), runs the
spending policy for value-moving ops, and dispatches. Payees and coordinators
act with plain keys through the `payee_*` methods.

Fees are not modelled; see whitepaper §5 for the fee market, paymaster and
zero-fee lane, which sit outside the state machines prototyped here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .accounts import AgentAccount, Attestation, Policy
from .channels import Channel, Transfer
from .crypto import PublicKey, Signed
from .errors import (
    InsufficientFunds,
    InvalidSignatureError,
    InvalidUpdate,
    NotFound,
    Unauthorized,
)
from .market import Contribution, ServiceOffer
from .pools import Pool


@dataclass
class Ledger:
    now: int = 1_700_000_000
    balances: dict[str, dict[str, int]] = field(default_factory=dict)
    accounts: dict[str, AgentAccount] = field(default_factory=dict)
    channels: dict[str, Channel] = field(default_factory=dict)
    pools: dict[str, Pool] = field(default_factory=dict)
    offers: dict[str, ServiceOffer] = field(default_factory=dict)
    contributions: dict[str, Contribution] = field(default_factory=dict)
    trusted_vendors: set[str] = field(default_factory=set)  # attestation roots (pubkey hex)
    log: list[dict] = field(default_factory=list)

    # --- balances ---------------------------------------------------------

    def balance(self, address: str, asset: str) -> int:
        return self.balances.get(address, {}).get(asset, 0)

    def mint(self, address: str, asset: str, amount: int) -> None:
        """Genesis / faucet. Not a transaction."""
        self.balances.setdefault(address, {})[asset] = self.balance(address, asset) + amount

    def _move(self, t: Transfer) -> None:
        if self.balance(t.src, t.asset) < t.amount:
            raise InsufficientFunds(f"{t.src} has {self.balance(t.src, t.asset)} {t.asset}, needs {t.amount}")
        self.balances[t.src][t.asset] -= t.amount
        self.balances.setdefault(t.dst, {})[t.asset] = self.balance(t.dst, t.asset) + t.amount
        self.log.append({"t": self.now, "transfer": t.__dict__})

    def _moves(self, ts: list[Transfer]) -> None:
        for t in ts:
            self._move(t)

    def advance(self, secs: int) -> None:
        self.now += secs

    def total_supply(self, asset: str) -> int:
        return sum(b.get(asset, 0) for b in self.balances.values())

    # --- accounts ---------------------------------------------------------

    def register_account(
        self,
        owner_signed: Signed,
        *,
        signer: PublicKey,
        policy: Policy,
        attestation: Optional[Attestation] = None,
        salt: int = 0,
    ) -> AgentAccount:
        """Owner signs {"op": "register", "signer": ..., "policy_id": ..., "salt": ...}."""
        if not owner_signed.valid():
            raise InvalidSignatureError("bad owner signature")
        b = owner_signed.body
        if b.get("op") != "register" or b.get("signer") != signer.to_dict() or b.get("policy_id") != policy.id or b.get("salt") != salt:
            raise InvalidUpdate("registration body does not match parameters")
        if attestation is not None:
            if attestation.signer != signer or not attestation.valid_for(self.trusted_vendors):
                raise Unauthorized("attestation invalid or not from a trusted vendor")
        acct_id = AgentAccount.make_id(owner_signed.signer, signer, salt)
        if acct_id in self.accounts:
            raise InvalidUpdate("account exists")
        acct = AgentAccount(acct_id, owner_signed.signer, signer, policy, attestation)
        self.accounts[acct_id] = acct
        return acct

    def _account(self, acct_id: str) -> AgentAccount:
        a = self.accounts.get(acct_id)
        if a is None:
            raise NotFound(f"no account {acct_id}")
        return a

    # --- the tree -----------------------------------------------------------

    def lineage(self, acct: AgentAccount) -> list[AgentAccount]:
        """The account, its parent, its grandparent, ... up to the root."""
        chain = [acct]
        while chain[-1].parent is not None:
            chain.append(self._account(chain[-1].parent))
        return chain

    def is_descendant(self, acct_id: str, ancestor_id: str) -> bool:
        a = self._account(acct_id)
        while a.parent is not None:
            if a.parent == ancestor_id:
                return True
            a = self._account(a.parent)
        return False

    def _tree_addresses(self, acct: AgentAccount) -> set[str]:
        root = self.lineage(acct)[-1]
        return {a.address for a in self.accounts.values()
                if a.id == root.id or self.is_descendant(a.id, root.id)}

    def _authorise(self, acct: AgentAccount, *, amount: int, payee: str, escalated: bool) -> None:
        """Value leaving the tree: every ancestor's policy must allow it, then every
        ancestor's window records it. All-or-nothing. Moves inside the tree are
        not spends and are not checked."""
        if payee in self._tree_addresses(acct):
            return
        chain = self.lineage(acct)
        for a in chain:
            spent = a.window.spent(self.now, a.policy.window_secs)
            # an escalation co-signature lifts per_tx_max only for the account whose
            # policy names that co-signer; ancestors' caps are never lifted from below
            a.policy.check(amount=amount, payee=payee, now=self.now, spent_in_window=spent,
                           escalated=escalated and a is acct)
        for a in chain:
            a.window.record(self.now, amount)

    def _may_administer(self, acct: AgentAccount, key: PublicKey) -> bool:
        """The owner, or the signer of any ancestor, may set policy, rotate the signer or recall."""
        if key == acct.owner:
            return True
        return any(key == a.signer for a in self.lineage(acct)[1:])

    def apply(self, env: Signed, escalation: Optional[Signed] = None) -> dict:
        """Apply an account-signed envelope. Returns a small result dict."""
        if not env.valid():
            raise InvalidSignatureError("bad envelope signature")
        b = env.body
        acct = self._account(b["account"])
        op = b["op"]
        owner_ops = {"rotate_signer", "set_policy"}
        if op in owner_ops:
            if not self._may_administer(acct, env.signer):
                raise Unauthorized(f"{op} must be signed by the account owner or an ancestor's signer")
        elif env.signer != acct.signer:
            raise Unauthorized(f"{op} must be signed by the account signer")
        if b.get("nonce") != acct.nonce:
            raise InvalidUpdate(f"nonce {b.get('nonce')} != expected {acct.nonce}")
        escalated = False
        if escalation is not None:
            if acct.policy.escalation is None or escalation.signer != acct.policy.escalation:
                raise Unauthorized("escalation signature not from the policy's escalation key")
            if not escalation.valid() or escalation.body != b:
                raise InvalidSignatureError("escalation must sign the same body")
            escalated = True
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            raise InvalidUpdate(f"unknown op {op}")
        result = handler(acct, b, escalated)
        acct.nonce += 1
        return result

    # value-moving ops: policy check via acct.authorise before any transfer

    def _require_funds(self, address: str, asset: str, amount: int) -> None:
        if self.balance(address, asset) < amount:
            raise InsufficientFunds(f"{address} has {self.balance(address, asset)} {asset}, needs {amount}")

    def _op_transfer(self, acct: AgentAccount, b: dict, escalated: bool) -> dict:
        self._require_funds(acct.address, b["asset"], b["amount"])  # validate before any window is recorded
        self._authorise(acct, amount=b["amount"], payee=b["to"], escalated=escalated)
        self._move(Transfer(acct.address, b["to"], b["asset"], b["amount"]))
        return {"ok": True}

    def _op_open_channel(self, acct: AgentAccount, b: dict, escalated: bool) -> dict:
        # the deposit is committed spend: the policy sees it at open, and the
        # signer sees each off-chain update; both bound the same money
        cid = Channel.make_id(acct.id, b["payee"], b["salt"])
        if cid in self.channels:
            raise InvalidUpdate("channel exists")
        self._require_funds(acct.address, b["asset"], b["deposit"])
        self._authorise(acct, amount=b["deposit"], payee=b["payee"], escalated=escalated)
        ch = Channel(cid, acct.id, b["payee"], b["asset"], b["deposit"], b["timeout_secs"])
        self._move(Transfer(acct.address, ch.escrow, b["asset"], b["deposit"]))
        self.channels[cid] = ch
        return {"channel_id": cid}

    def _op_close_channel(self, acct: AgentAccount, b: dict, _: bool) -> dict:
        ch = self._channel(b["channel_id"])
        if ch.payer_account != acct.id:
            raise Unauthorized("not the channel's payer")
        latest = Signed.from_dict(b["latest"]) if b.get("latest") else None
        self._moves(ch.begin_close(self.now, latest, account_id=acct.id, signer=acct.signer))
        return {"closing_at": ch.closing_at}

    def _op_finalize_close(self, acct: AgentAccount, b: dict, _: bool) -> dict:
        ch = self._channel(b["channel_id"])
        if ch.payer_account != acct.id:
            raise Unauthorized("not the channel's payer")
        self._moves(ch.finalize_close(self.now))
        return {"closed": True}

    def _op_join_pool(self, acct: AgentAccount, b: dict, escalated: bool) -> dict:
        pool = self._pool(b["pool_id"])
        active = acct.id in pool.members and not pool.members[acct.id].exited
        if active or b["deposit"] <= 0:
            raise InvalidUpdate("already a member" if active else "deposit must be positive")
        self._require_funds(acct.address, pool.asset, b["deposit"])
        self._authorise(acct, amount=b["deposit"], payee=pool.coordinator, escalated=escalated)
        pool.join(acct.id, b["deposit"])
        self._move(Transfer(acct.address, pool.escrow, pool.asset, b["deposit"]))
        return {"ok": True}

    def _op_begin_exit(self, acct: AgentAccount, b: dict, _: bool) -> dict:
        pool = self._pool(b["pool_id"])
        latest = Signed.from_dict(b["latest"]) if b.get("latest") else None
        self._moves(pool.begin_exit(self.now, acct.id, latest, acct.signer))
        return {"exit_at": pool.members[acct.id].exit_at}

    def _op_finalize_exit(self, acct: AgentAccount, b: dict, _: bool) -> dict:
        pool = self._pool(b["pool_id"])
        self._moves(pool.finalize_exit(self.now, acct.id))
        return {"exited": True}

    def _op_rotate_signer(self, acct: AgentAccount, b: dict, _: bool) -> dict:
        acct.signer = PublicKey.from_dict(b["new_signer"])
        acct.attestation = None  # a new key needs a new quote
        return {"ok": True}

    def _op_set_policy(self, acct: AgentAccount, b: dict, _: bool) -> dict:
        policy = Policy.from_dict(b["policy"])
        if acct.parent is not None:
            policy.within(self._account(acct.parent).policy)
        acct.policy = policy
        return {"ok": True}

    # --- delegation (signer ops on the parent) ------------------------------

    def _op_delegate(self, acct: AgentAccount, b: dict, _: bool) -> dict:
        """Create a child account under this one, with a policy within this one's,
        and move `fund` of `asset` to it. Funding a child is not a spend."""
        signer = PublicKey.from_dict(b["signer"])
        policy = Policy.from_dict(b["policy"])
        policy.within(acct.policy)
        cid = AgentAccount.make_child_id(acct.id, signer, b.get("salt", 0))
        if cid in self.accounts:
            raise InvalidUpdate("child account exists")
        child = AgentAccount(cid, acct.owner, signer, policy, None, parent=acct.id)
        self.accounts[cid] = child
        if b.get("fund"):
            self._move(Transfer(acct.address, child.address, b["asset"], b["fund"]))
        return {"account_id": cid}

    def _op_recall(self, acct: AgentAccount, b: dict, _: bool) -> dict:
        """Pull a descendant's free balance back up to this account. Not a spend."""
        child = self._account(b["child"])
        if not self.is_descendant(child.id, acct.id):
            raise Unauthorized("recall target is not a descendant")
        amount = b["amount"] if b.get("amount") is not None else self.balance(child.address, b["asset"])
        if amount:
            self._move(Transfer(child.address, acct.address, b["asset"], amount))
        return {"recalled": amount}

    # --- payee / coordinator side (plain keys) ----------------------------

    def _channel(self, cid: str) -> Channel:
        ch = self.channels.get(cid)
        if ch is None:
            raise NotFound(f"no channel {cid}")
        return ch

    def _pool(self, pid: str) -> Pool:
        p = self.pools.get(pid)
        if p is None:
            raise NotFound(f"no pool {pid}")
        return p

    def payee_settle_channel(self, cid: str, update: Signed) -> int:
        """Anyone may submit a payer-signed update; returns the amount paid out."""
        ch = self._channel(cid)
        acct = self._account(ch.payer_account)
        effects = ch.settle(update, account_id=acct.id, signer=acct.signer)
        self._moves(effects)
        return sum(t.amount for t in effects)

    def payee_contest_close(self, cid: str, update: Signed) -> int:
        """Same as settle, allowed during the close window."""
        ch = self._channel(cid)
        if ch.closing_at is None or self.now >= ch.closing_at:
            raise InvalidUpdate("no close window open")
        return self.payee_settle_channel(cid, update)

    def payee_claim_stream(self, cid: str) -> int:
        ch = self._channel(cid)
        effects = ch.claim_stream(self.now)
        self._moves(effects)
        return sum(t.amount for t in effects)

    def start_stream(self, cid: str, signed: Signed) -> None:
        ch = self._channel(cid)
        acct = self._account(ch.payer_account)
        ch.start_stream(signed, account_id=acct.id, signer=acct.signer)

    def stop_stream(self, cid: str) -> None:
        self._channel(cid).stop_stream(self.now)

    def create_pool(self, coordinator: str, asset: str, timeout_secs: int, bond: int, salt: int = 0) -> Pool:
        pid = Pool.make_id(coordinator, salt)
        if pid in self.pools:
            raise InvalidUpdate("pool exists")
        pool = Pool(pid, coordinator, asset, timeout_secs, bond)
        if bond:
            self._move(Transfer(coordinator, pool.bond_escrow, asset, bond))
        self.pools[pid] = pool
        return pool

    def coordinator_settle_pool(self, pid: str, updates: list[Signed]) -> int:
        pool = self._pool(pid)
        triples = []
        for u in updates:
            acct = self._account(u.body["account"])
            triples.append((u, acct.id, acct.signer))
        effects = pool.settle(triples)
        self._moves(effects)
        return sum(t.amount for t in effects)

    def coordinator_contest_exit(self, pid: str, update: Signed) -> int:
        pool = self._pool(pid)
        acct = self._account(update.body["account"])
        effects = pool.contest_exit(self.now, acct.id, update, acct.signer)
        self._moves(effects)
        return sum(t.amount for t in effects)

    # --- market -----------------------------------------------------------

    def publish_offer(self, offer: ServiceOffer) -> str:
        self.offers[offer.id] = offer
        return offer.id

    def register_contribution(self, c: Contribution) -> str:
        if c.parent is not None and c.parent not in self.contributions:
            raise NotFound(f"parent {c.parent} unknown")
        self.contributions[c.id] = c
        return c.id
