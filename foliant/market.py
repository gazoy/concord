"""Compute and data market objects (whitepaper §6.7, §6.11).

ServiceOffer: what a provider sells, at what price, with what attestation
requirement. Receipt: a channel/pool update that also commits to a request and
response, optionally with a proof reference. Contribution: a data or model
contribution with a licence and a royalty split that cascades to derivatives.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .crypto import Signed, hash_obj


@dataclass(frozen=True)
class ServiceOffer:
    provider: str  # payee address
    asset: str
    price_per_unit: int
    unit: str  # "call", "token", "second", ...
    descriptor: dict = field(default_factory=dict)  # model id, endpoint, terms
    required_code_hash: Optional[str] = None  # attestation the payer must present
    pool_id: Optional[str] = None  # if the provider runs a pool, payers may join it

    @property
    def id(self) -> str:
        return hash_obj(self.to_dict())

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "asset": self.asset,
            "price_per_unit": self.price_per_unit,
            "unit": self.unit,
            "descriptor": self.descriptor,
            "required_code_hash": self.required_code_hash,
            "pool_id": self.pool_id,
        }


@dataclass(frozen=True)
class Receipt:
    """Binds a payment update to what was bought. Kept by both sides; attachable on-chain."""

    update: Signed  # the channel or pool update that paid for this unit
    request_hash: str
    response_hash: str
    provider_sig: Optional[Signed] = None  # provider acknowledges the response hash
    proof_ref: Optional[str] = None  # zkML/opML proof locator, if any

    @property
    def id(self) -> str:
        return hash_obj({"u": self.update.to_dict(), "q": self.request_hash, "r": self.response_hash})


@dataclass
class Contribution:
    id: str
    contributor: str  # address
    licence: str  # SPDX-style identifier or URI
    royalty_bps: int  # share of derivative revenue owed to this node
    parent: Optional[str] = None  # id of the contribution this derives from

    @staticmethod
    def make_id(contributor: str, content_hash: str) -> str:
        return hash_obj({"c": contributor, "h": content_hash})


def royalty_split(graph: dict[str, Contribution], leaf_id: str, revenue: int) -> dict[str, int]:
    """Cascade `revenue` earned by `leaf_id` up its parent chain.

    Each ancestor takes its royalty_bps of what reaches it; the remainder
    continues to the next parent. The leaf keeps whatever is left. Integer
    arithmetic; dust stays with the leaf.
    """
    shares: dict[str, int] = {}
    node = graph[leaf_id]
    remaining = revenue
    parent_id = node.parent
    while parent_id is not None:
        parent = graph[parent_id]
        cut = remaining * parent.royalty_bps // 10_000
        shares[parent.contributor] = shares.get(parent.contributor, 0) + cut
        remaining -= cut
        parent_id = parent.parent
    shares[node.contributor] = shares.get(node.contributor, 0) + remaining
    return shares
