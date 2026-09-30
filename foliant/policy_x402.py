"""Spending-policy binding for the plain x402 `exact` scheme (docs/spec/spending-policy.md §7.2).

A Foliant policy bounds *committed value*. For the channel and pool schemes the
commitment is the deposit and the off-chain updates need no further check. For
the plain x402 `exact` scheme every payment is its own commitment, so every
payment is a spend and is checked as one. This module turns an x402 exact
payment (v1 or v2 wire shape; EVM payloads only: EIP-3009 or Permit2) into the
`(amount, payee)` the policy sees, and checks it against a policy chain the
way the ledger checks a transfer: every ancestor, all or nothing.

It deliberately does not verify the payment's own signature, nonce, balance or
deadline: those are the facilitator's job under the x402 specification and are
unchanged by a policy. A policy check sits before them (wallet side: should I
sign this?) or beside them (facilitator side: is this payer within policy?).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from .accounts import Policy, canonical_address
from .errors import PolicyViolation

SCHEME = "exact"
UINT = re.compile(r"0|[1-9][0-9]*")

# The x402 Permit2 proxy that honours `witness.to`. A Permit2 payment naming any other
# spender grants that spender the tokens, whatever the witness says (REVIEW-1 S-10). The
# reference SDK deploys one proxy at the same address on every supported EVM network.
PERMIT2_PROXY = "0x402085c248eea27d92e8b30b2c58ed07f9e20001"


@dataclass(frozen=True)
class Spend:
    """What a policy sees of an exact payment."""

    amount: int  # smallest unit of `asset`
    payee: str  # requirements.payTo, canonical
    asset: str
    network: str
    payer: Optional[str]  # canonical, when the payload names one


def _uint(v, what: str) -> int:
    """Spec §7.2 amount grammar: a decimal string matching ^(0|[1-9][0-9]*)$."""
    if not isinstance(v, str) or not UINT.fullmatch(v):
        raise PolicyViolation(f"{what} is not a canonical decimal string", "malformed_payment")
    return int(v)


def _addr(v, what: str) -> str:
    if not isinstance(v, str) or not v:
        raise PolicyViolation(f"{what} is not an address string", "malformed_payment")
    return canonical_address(v)


def _canon_req(r: dict) -> tuple:
    """The fields that identify what is being paid (spec §7.2): scheme, network, asset,
    amount, payTo and extra (the EIP-712 domain lives there and changes what a signature
    covers). maxTimeoutSeconds and resource metadata are not part of the payment."""
    return (r.get("scheme"), r.get("network"), canonical_address(str(r.get("asset", ""))),
            str(r.get("amount", r.get("maxAmountRequired"))), canonical_address(str(r.get("payTo", ""))),
            _frozen(r.get("extra")))


def _frozen(v):
    if isinstance(v, dict):
        return frozenset((k, _frozen(x)) for k, x in v.items())
    if isinstance(v, list):
        return tuple(_frozen(x) for x in v)
    return v


def spend_of_exact(payment: dict, requirements: Optional[dict] = None) -> Spend:
    """Extract the spend from an x402 exact PaymentPayload.

    `payment` is the decoded PAYMENT-SIGNATURE (v2) or X-PAYMENT (v1) body.
    `requirements` is the PaymentRequirements being paid; for v2 it may be
    omitted, in which case `payment["accepted"]` is used, and if given it must
    agree with `accepted`.

    Raises PolicyViolation with a §7.2 reason code, in the precedence the spec
    lists: malformed_payment (shape), requirements_mismatch, unsupported_scheme,
    malformed_payment (fields), spender_mismatch, asset_mismatch, payee_mismatch,
    amount_mismatch.
    """
    version = payment.get("x402Version") if isinstance(payment, dict) else None
    if version == 2:
        accepted = payment.get("accepted")
        if not isinstance(accepted, dict):
            raise PolicyViolation("v2 payment without accepted requirements", "malformed_payment")
        if requirements is not None and _canon_req(requirements) != _canon_req(accepted):
            raise PolicyViolation("requirements do not match payment.accepted", "requirements_mismatch")
        req = accepted
        amount_field = "amount"
    elif version == 1:
        if requirements is None:
            raise PolicyViolation("v1 payment needs its requirements", "malformed_payment")
        req = requirements
        if payment.get("scheme") != req.get("scheme") or payment.get("network") != req.get("network"):
            raise PolicyViolation("scheme or network differs from requirements", "requirements_mismatch")
        amount_field = "maxAmountRequired"
    else:
        raise PolicyViolation(f"unsupported x402Version {version!r}", "malformed_payment")

    if req.get("scheme") != SCHEME:
        raise PolicyViolation(f"scheme {req.get('scheme')!r} is not exact", "unsupported_scheme")
    try:
        required = _uint(req[amount_field], amount_field)
        pay_to = _addr(req["payTo"], "payTo")
        asset = _addr(req["asset"], "asset")
        network = req["network"]
        if not isinstance(network, str):
            raise KeyError("network")
    except KeyError as e:
        raise PolicyViolation(f"malformed requirements: missing {e}", "malformed_payment")

    body = payment.get("payload")
    if not isinstance(body, dict):
        raise PolicyViolation("payment without payload", "malformed_payment")
    try:
        if "permit2Authorization" in body:
            a = body["permit2Authorization"]
            value = _uint(a["permitted"]["amount"], "permitted.amount")
            to = _addr(a["witness"]["to"], "witness.to")
            token = _addr(a["permitted"]["token"], "permitted.token")
            spender = _addr(a["spender"], "spender")
            payer = _addr(a["from"], "from")
            if spender != PERMIT2_PROXY:
                raise PolicyViolation("permit2 spender is not the x402 proxy: witness.to would not bind", "spender_mismatch")
            if token != asset:
                raise PolicyViolation("permit2 token differs from requirements.asset", "asset_mismatch")
        elif "authorization" in body:
            a = body["authorization"]
            value = _uint(a["value"], "authorization.value")
            to = _addr(a["to"], "authorization.to")
            payer = _addr(a["from"], "authorization.from")
        else:
            raise PolicyViolation("not an EVM exact payload (EIP-3009 or Permit2)", "malformed_payment")
    except (KeyError, TypeError) as e:
        raise PolicyViolation(f"malformed payload: {e}", "malformed_payment")

    if to != pay_to:
        raise PolicyViolation("payload recipient differs from requirements.payTo", "payee_mismatch")
    if value != required:
        # the exact scheme pays exactly the required amount; a policy never sees a smaller figure
        raise PolicyViolation(f"payload value {value} differs from required {required}", "amount_mismatch")
    return Spend(amount=value, payee=pay_to, asset=asset, network=network, payer=payer)


def check_exact(
    payment: dict,
    requirements: Optional[dict],
    chain: Sequence[Policy],
    spent_in_window: Sequence[int],
    now: int,
    *,
    escalated: bool = False,
    expected_payer: Optional[str] = None,
) -> Spend:
    """Check an exact payment against a policy chain (the paying account first, then each
    ancestor up to the root), exactly as the ledger checks a transfer (§4.2): every policy
    must allow it, each against its own window; `escalated` lifts per_tx_max for the first
    policy only. `expected_payer` is the payment address of the account being evaluated;
    when given, a payload from any other address is `payer_mismatch` (an evaluator must
    not let a payer charge another account's window). Returns the Spend on success; raises
    PolicyViolation otherwise. Recording the spend in each window is the caller's job, and
    only after the payment is accepted.
    """
    if len(chain) != len(spent_in_window) or not chain:
        raise ValueError("one spent_in_window figure per policy, at least one policy")
    if escalated and chain[0].escalation is None:
        raise ValueError("escalated without an escalation co-signer on the policy")
    spend = spend_of_exact(payment, requirements)
    if expected_payer is not None and spend.payer != canonical_address(expected_payer):
        raise PolicyViolation("payload payer is not the account being evaluated", "payer_mismatch")
    for i, (policy, spent) in enumerate(zip(chain, spent_in_window)):
        policy.check(amount=spend.amount, payee=spend.payee, now=now, spent_in_window=spent,
                     escalated=escalated and i == 0)
    return spend
