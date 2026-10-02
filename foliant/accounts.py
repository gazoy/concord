"""Agent accounts, spending policies and attestation (whitepaper §6.1-6.3).

An AgentAccount is operated by a `signer` key under a `Policy`. The policy is
checked by the ledger (on-chain) for every value-moving transaction, and by
the AgentSigner (the enclave side) for every off-chain channel or pool update.
Both sides run the same `Policy.check`, so a signer that refuses to sign and a
ledger that refuses to apply agree exactly.

Accounts form a tree (whitepaper §6.2, hierarchical budgets). An account's
signer may `delegate`: create a child account with its own signer and a policy
that sits within the parent's (`Policy.within`). Value leaving the tree is
checked against, and recorded in, every ancestor's policy and window, so no
branch can ever commit more than any ancestor allows, whatever its own policy
says. Moves inside the tree (delegating funds down, recalling them up) are not
spends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .crypto import KeyPair, PublicKey, Signed, hash_obj, sign
from .errors import PolicyViolation


MAX_WINDOW_SECS = 30 * 86400  # this reference bounds windowSecs (spec §2); the EVM reference accepts any uint32
UINT128_MAX = (1 << 128) - 1
# Spec §2/§2.2: a policy has exactly these seven fields (the schema sets "additionalProperties": false).
# A loader that dropped a field it did not understand would enforce less than the owner signed.
WIRE_FIELDS = frozenset({
    "perTxMax", "perWindowMax", "windowSecs", "allowList", "denyList", "expiry", "escalation",
})
DICT_FIELDS = frozenset({
    "per_tx_max", "per_window_max", "window_secs", "allow_list", "deny_list", "expiry", "escalation",
})


def _reject_unknown(d: object, allowed: frozenset[str]) -> dict:
    """Spec §2: a policy carrying a field outside the seven is invalid at load, not silently
    stripped — dropping it would enforce less than the owner signed over."""
    if not isinstance(d, dict):
        raise PolicyViolation(f"policy must be an object, not {type(d).__name__}", "policy_invalid")
    unknown = sorted(repr(k) for k in set(d) - allowed)
    if unknown:
        raise PolicyViolation(f"unknown policy field(s): {', '.join(unknown)}", "policy_invalid")
    return d


def canonical_address(a: str) -> str:
    """Spec §2.1: EVM addresses compare in lowercase hex; other address forms as given."""
    return a.lower() if a.startswith("0x") or a.startswith("0X") else a


@dataclass
class Policy:
    per_tx_max: int
    per_window_max: int
    window_secs: int
    allow_list: Optional[frozenset[str]] = None  # payee addresses; None = any
    deny_list: frozenset[str] = frozenset()
    expiry: Optional[int] = None  # unix seconds; None = never
    escalation: Optional[PublicKey] = None  # co-signer that may exceed per_tx_max

    def __post_init__(self) -> None:
        """Reject a policy the spec calls invalid (§2) and canonicalise its lists (§2.1)."""
        for name in ("per_tx_max", "per_window_max"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > UINT128_MAX:
                raise PolicyViolation(f"{name} must be an integer in [0, 2^128)", "policy_invalid")
        if not isinstance(self.window_secs, int) or isinstance(self.window_secs, bool) \
                or self.window_secs < 1 or self.window_secs > MAX_WINDOW_SECS:
            raise PolicyViolation(f"window_secs must be in [1, {MAX_WINDOW_SECS}]", "policy_invalid")
        if self.expiry is not None and (not isinstance(self.expiry, int) or isinstance(self.expiry, bool) or self.expiry < 1):
            raise PolicyViolation("expiry must be null or an integer >= 1", "policy_invalid")
        if self.allow_list is not None:
            self.allow_list = frozenset(canonical_address(a) for a in self.allow_list)
        self.deny_list = frozenset(canonical_address(a) for a in self.deny_list)
        if isinstance(self.escalation, str):  # address-form co-signer (spec §2): canonical, never the zero address
            self.escalation = canonical_address(self.escalation)
            if self.escalation == "0x" + "0" * 40:
                raise PolicyViolation("escalation must not be the zero address", "policy_invalid")

    @property
    def id(self) -> str:
        """Id of this policy as this reference encodes it in envelopes (`to_dict`). For the
        specification's wire-form id see `spec_id`."""
        return hash_obj(self.to_dict())

    def wire(self) -> dict:
        """The policy in the specification's wire form (spending-policy.md §2): camelCase keys,
        amounts as decimal strings, lists sorted, null fields present."""
        esc = self.escalation
        return {
            "perTxMax": str(self.per_tx_max),
            "perWindowMax": str(self.per_window_max),
            "windowSecs": self.window_secs,
            "allowList": sorted(self.allow_list) if self.allow_list is not None else None,
            "denyList": sorted(self.deny_list),
            "expiry": self.expiry,
            "escalation": esc.to_dict() if isinstance(esc, PublicKey) else esc,
        }

    @property
    def spec_id(self) -> str:
        """Spec §2.2: SHA-256 of the canonical encoding of the wire form."""
        return hash_obj(self.wire())

    @classmethod
    def from_wire(cls, d: dict) -> "Policy":
        """Parse the specification's wire form. Amount strings must match the spec's uint grammar,
        and a field outside the seven makes the policy invalid (§2, schema `additionalProperties`
        false) rather than being dropped."""
        import re
        _reject_unknown(d, WIRE_FIELDS)
        try:
            for k in ("perTxMax", "perWindowMax"):
                if not isinstance(d[k], str) or not re.fullmatch(r"0|[1-9][0-9]*", d[k]):
                    raise PolicyViolation(f"{k} must be a decimal string", "policy_invalid")
            esc = d["escalation"]
            if isinstance(esc, dict):
                esc = PublicKey.from_dict(esc)
            return cls(
                per_tx_max=int(d["perTxMax"]),
                per_window_max=int(d["perWindowMax"]),
                window_secs=d["windowSecs"],
                allow_list=frozenset(d["allowList"]) if d["allowList"] is not None else None,
                deny_list=frozenset(d["denyList"]),
                expiry=d["expiry"],
                escalation=esc,
            )
        except (KeyError, TypeError) as e:
            raise PolicyViolation(f"malformed policy: {e}", "policy_invalid")

    def to_dict(self) -> dict:
        return {
            "per_tx_max": self.per_tx_max,
            "per_window_max": self.per_window_max,
            "window_secs": self.window_secs,
            "allow_list": sorted(self.allow_list) if self.allow_list is not None else None,
            "deny_list": sorted(self.deny_list),
            "expiry": self.expiry,
            "escalation": self.escalation.to_dict() if self.escalation else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        """Parse this reference's envelope encoding. Same rule as `from_wire`: a field outside the
        seven is invalid, so a dropped field cannot make enforcement weaker than what was signed."""
        _reject_unknown(d, DICT_FIELDS)
        return cls(
            per_tx_max=d["per_tx_max"],
            per_window_max=d["per_window_max"],
            window_secs=d["window_secs"],
            allow_list=frozenset(d["allow_list"]) if d.get("allow_list") is not None else None,
            deny_list=frozenset(d.get("deny_list", [])),
            expiry=d.get("expiry"),
            escalation=PublicKey.from_dict(d["escalation"]) if d.get("escalation") else None,
        )

    def check(
        self,
        *,
        amount: int,
        payee: str,
        now: int,
        spent_in_window: int,
        escalated: bool = False,
    ) -> None:
        """Raise PolicyViolation if a payment of `amount` to `payee` is not allowed."""
        payee = canonical_address(payee)
        if amount < 0:
            raise PolicyViolation("negative amount", "negative_amount")
        if self.expiry is not None and now >= self.expiry:
            raise PolicyViolation("policy expired", "expired")
        if payee in self.deny_list:
            raise PolicyViolation(f"payee {payee} is denied", "payee_denied")
        if self.allow_list is not None and payee not in self.allow_list:
            raise PolicyViolation(f"payee {payee} is not on the allow list", "payee_not_allowed")
        if amount > self.per_tx_max and not escalated:
            raise PolicyViolation(f"amount {amount} exceeds per_tx_max {self.per_tx_max}", "per_tx_exceeded")
        if spent_in_window + amount > self.per_window_max:
            raise PolicyViolation(
                f"amount {amount} would exceed per_window_max {self.per_window_max} "
                f"(already spent {spent_in_window})",
                "per_window_exceeded",
            )

    def within(self, parent: "Policy") -> None:
        """Raise PolicyViolation unless this policy is no wider than `parent` on every axis.

        Runtime enforcement up the tree makes this a guarantee at delegation time
        rather than the only line of defence: a child that somehow held a wider
        policy would still be bounded by its ancestors at every spend.

        Not compared, deliberately: `window_secs` (a child may meter over a shorter
        window than its parent; the parent's window still binds the subtree) and
        `escalation` (a child's co-signer lifts only the child's own per_tx_max;
        see Ledger._authorise).
        """
        if self.per_tx_max > parent.per_tx_max:
            raise PolicyViolation(f"child per_tx_max {self.per_tx_max} exceeds parent {parent.per_tx_max}", "child_per_tx_wider")
        if self.per_window_max > parent.per_window_max:
            raise PolicyViolation(f"child per_window_max {self.per_window_max} exceeds parent {parent.per_window_max}", "child_per_window_wider")
        if parent.allow_list is not None and (self.allow_list is None or not self.allow_list <= parent.allow_list):
            raise PolicyViolation("child allow list must be a subset of the parent's", "child_allow_wider")
        if not parent.deny_list <= self.deny_list:
            raise PolicyViolation("child deny list must include the parent's", "child_deny_narrower")
        if parent.expiry is not None and (self.expiry is None or self.expiry > parent.expiry):
            raise PolicyViolation("child expiry must not be later than the parent's", "child_expiry_later")


@dataclass(frozen=True)
class Attestation:
    """A remote-attestation quote binding `signer` to `code_hash`, signed by a vendor key.

    Simulated: the vendor is any key the ledger has registered as a trusted
    attestation root. Real quotes (TDX, SEV-SNP, Nitro) are verified by a
    precompile; the shape is the same.
    """

    code_hash: str
    signer: PublicKey
    quote: Signed

    @staticmethod
    def issue(vendor: KeyPair, code_hash: str, signer: PublicKey) -> "Attestation":
        body = {"code_hash": code_hash, "signer": signer.to_dict()}
        return Attestation(code_hash, signer, sign(vendor, body))

    def valid_for(self, trusted_vendors: set[str]) -> bool:
        return (
            self.quote.valid()
            and self.quote.signer.hex in trusted_vendors
            and self.quote.body == {"code_hash": self.code_hash, "signer": self.signer.to_dict()}
        )


@dataclass
class SpendWindow:
    """Rolling record of value the account has committed, for per_window_max.

    Spec §6.1: a spend recorded at t counts at `now` unless it has aged out under a window in
    force at any time since t. Because the window is piecewise constant, that is decided by
    pruning under the old window at each window change (`rewindow`) and under the current
    window at evaluation (`spent`); pruning is permanent, so a later, longer window never
    brings a dropped spend back. The result depends only on the log and the policy history,
    never on when `spent` happened to be called.
    """

    entries: list[tuple[int, int]] = field(default_factory=list)  # (ts, amount)

    def spent(self, now: int, window_secs: int) -> int:
        cutoff = now - window_secs
        self.entries = [(t, a) for t, a in self.entries if t > cutoff]
        return sum(a for _, a in self.entries)

    def rewindow(self, changed_at: int, old_window_secs: int) -> None:
        """Apply a window-length change: prune under the outgoing length as of `changed_at`, the
        time the change took effect (spec §6.1: the outgoing length applies at that instant)."""
        self.spent(changed_at, old_window_secs)

    def record(self, now: int, amount: int) -> None:
        self.entries.append((now, amount))


@dataclass
class AgentAccount:
    id: str
    owner: PublicKey
    signer: PublicKey
    policy: Policy
    attestation: Optional[Attestation] = None
    nonce: int = 0
    window: SpendWindow = field(default_factory=SpendWindow)
    parent: Optional[str] = None  # account id this one was delegated from; None = root

    @staticmethod
    def make_id(owner: PublicKey, signer: PublicKey, nonce_salt: int) -> str:
        return hash_obj({"owner": owner.to_dict(), "signer": signer.to_dict(), "salt": nonce_salt})

    @staticmethod
    def make_child_id(parent_id: str, signer: PublicKey, nonce_salt: int) -> str:
        return hash_obj({"parent": parent_id, "signer": signer.to_dict(), "salt": nonce_salt})

    @property
    def address(self) -> str:
        """Where the account's funds sit on the ledger."""
        return f"account:{self.id}"

    def authorise(self, *, amount: int, payee: str, now: int, escalated: bool = False) -> None:
        """Policy check then record the spend. Raises PolicyViolation without recording."""
        spent = self.window.spent(now, self.policy.window_secs)
        self.policy.check(
            amount=amount, payee=payee, now=now, spent_in_window=spent, escalated=escalated
        )
        self.window.record(now, amount)


class AgentSigner:
    """The enclave side: holds the signer key and refuses to sign anything the policy forbids.

    It keeps its own SpendWindow so per_window_max holds across off-chain updates
    that the ledger never sees individually.
    """

    def __init__(self, keypair: KeyPair, policy: Policy, account_id: str):
        self.keypair = keypair
        self.policy = policy
        self.account_id = account_id
        self.window = SpendWindow()
        self._last_balance: dict[str, int] = {}  # channel/pool id -> last signed balance

    @property
    def public(self) -> PublicKey:
        return self.keypair.public

    def set_policy(self, policy: Policy, changed_at: int) -> None:
        """Replace the policy (an administrator changed it). Spec §6.1: prune under the outgoing
        window as of the moment the change took effect on the ledger, `changed_at`, not as of the
        moment this signer learns of it, or the two sides diverge (REVIEW-1 round 2, S-1b)."""
        if policy.window_secs != self.policy.window_secs:
            self.window.rewindow(changed_at, self.policy.window_secs)
        self.policy = policy

    def sign_payment(self, *, payee: str, amount: int, now: int, body: dict, escalated: bool = False) -> Signed:
        """Sign `body` if committing `amount` to `payee` is within policy."""
        spent = self.window.spent(now, self.policy.window_secs)
        self.policy.check(amount=amount, payee=payee, now=now, spent_in_window=spent, escalated=escalated)
        self.window.record(now, amount)
        return sign(self.keypair, body)

    def sign_update(self, *, kind: str, obj_id: str, payee: str, seq: int, balance: int, now: int, epoch: Optional[int] = None) -> Signed:
        """Sign a channel or pool update.

        The policy bounds *committed* value: the deposit was authorised when the
        channel or pool was funded, and every update is bounded by that deposit,
        so an update is not a new spend. The signer still enforces the payee
        allow/deny lists, expiry, and monotonic balances.
        """
        last = self._last_balance.get(obj_id, 0)
        if balance < last:
            raise PolicyViolation("balance must not decrease")
        spent = self.window.spent(now, self.policy.window_secs)
        self.policy.check(amount=0, payee=payee, now=now, spent_in_window=spent)
        body = {"kind": kind, "id": obj_id, "seq": seq, "balance": balance, "account": self.account_id}
        if epoch is not None:
            body["epoch"] = epoch  # pool claims: which membership this update belongs to
        signed = sign(self.keypair, body)
        self._last_balance[obj_id] = balance
        return signed

    def forget(self, obj_id: str) -> None:
        """Drop the monotonic-balance record for a closed channel or exited pool claim."""
        self._last_balance.pop(obj_id, None)

    def sign_plain(self, body: dict) -> Signed:
        """Sign a non-payment message (account maintenance, exits, receipts)."""
        return sign(self.keypair, body)
