"""Keys, signatures and canonical hashing.

Ed25519 stands in for the production scheme. Whitepaper principle 9 requires
signature agility, so every signed message carries a `scheme` tag and the
verifier dispatches on it; only "ed25519" is implemented here.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

from .errors import InvalidKey

SCHEME = "ed25519"

# Spec §2.2: a key reference is exactly {"scheme": <name>, "key": <lowercase hex>}. The schema
# says the same (`$defs.keyRef`, additionalProperties false, pattern ^[0-9a-f]+$).
KEYREF_FIELDS = frozenset({"scheme", "key"})
# Public key length per scheme, for the schemes this implementation knows. A scheme absent here
# still parses -- signature agility means an envelope can name a scheme we cannot verify, and
# `verify` is where that is refused -- but its length cannot be checked.
KEY_BYTES = {SCHEME: 32}
_HEX = re.compile(r"(?:[0-9a-f]{2})+")


def canonical(obj: Any) -> bytes:
    """Deterministic JSON encoding used for every signed or hashed message."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj: Any) -> str:
    return sha256(canonical(obj))


@dataclass(frozen=True)
class PublicKey:
    scheme: str
    raw: bytes

    @property
    def hex(self) -> str:
        return self.raw.hex()

    @property
    def address(self) -> str:
        """Address = first 20 bytes of sha256(scheme || pubkey)."""
        return sha256(self.scheme.encode() + self.raw)[:40]

    def verify(self, message: bytes, signature: bytes) -> bool:
        if self.scheme != SCHEME:
            # a FoliantError, not a ValueError: this is reachable from the wire (an envelope
            # naming a scheme this node does not implement) and so must answer 400, not 500
            raise InvalidKey(f"unsupported scheme {self.scheme}")
        try:
            Ed25519PublicKey.from_public_bytes(self.raw).verify(signature, message)
            return True
        except InvalidSignature:
            return False

    def to_dict(self) -> dict:
        return {"scheme": self.scheme, "key": self.hex}

    @classmethod
    def from_dict(cls, d: dict) -> "PublicKey":
        """Parse a key reference from the wire (spec §2.2).

        Every owner, signer and escalation co-signer key enters here, so each of these used to be
        a 500: `bytes.fromhex` raised ValueError on bad hex and KeyError on a missing `key`, and
        neither is a FoliantError. A two-byte string was accepted as an ed25519 public key, and an
        unknown nested field was dropped although the schema forbids it -- which, for a key inside
        a signed policy, meant the id covered less than the signer wrote.

        Stricter than the schema in one way it cannot express: hex of odd length matches
        `^[0-9a-f]+$` but is not a whole number of bytes, so it is refused here.
        """
        if not isinstance(d, dict):
            raise InvalidKey(f"key reference must be an object, not {type(d).__name__}")
        unknown = sorted(repr(k) for k in set(d) - KEYREF_FIELDS)
        if unknown:
            raise InvalidKey(f"unknown key reference field(s): {', '.join(unknown)}")
        missing = sorted(repr(k) for k in KEYREF_FIELDS - set(d))
        if missing:
            raise InvalidKey(f"missing key reference field(s): {', '.join(missing)}")
        scheme, key = d["scheme"], d["key"]
        if not isinstance(scheme, str) or not scheme:
            raise InvalidKey("scheme must be a non-empty string")
        if not isinstance(key, str) or not _HEX.fullmatch(key):
            raise InvalidKey("key must be a non-empty even-length lowercase hex string")
        raw = bytes.fromhex(key)
        want = KEY_BYTES.get(scheme)
        if want is not None and len(raw) != want:
            raise InvalidKey(f"a {scheme} key is {want} bytes, not {len(raw)}")
        return cls(scheme, raw)


class KeyPair:
    def __init__(self, private: Ed25519PrivateKey | None = None):
        self._priv = private or Ed25519PrivateKey.generate()
        raw = self._priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.public = PublicKey(SCHEME, raw)

    @classmethod
    def from_seed(cls, seed: bytes) -> "KeyPair":
        return cls(Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed).digest()))

    @property
    def address(self) -> str:
        return self.public.address

    def sign(self, message: bytes) -> bytes:
        return self._priv.sign(message)

    def sign_obj(self, obj: Any) -> str:
        return self.sign(canonical(obj)).hex()


@dataclass(frozen=True)
class Signed:
    """A message plus one signature. `body` must be JSON-serialisable."""

    body: dict
    signer: PublicKey
    signature: str

    def valid(self) -> bool:
        return self.signer.verify(canonical(self.body), bytes.fromhex(self.signature))

    def to_dict(self) -> dict:
        return {"body": self.body, "signer": self.signer.to_dict(), "signature": self.signature}

    @classmethod
    def from_dict(cls, d: dict) -> "Signed":
        return cls(d["body"], PublicKey.from_dict(d["signer"]), d["signature"])


def sign(kp: KeyPair, body: dict) -> Signed:
    return Signed(body, kp.public, kp.sign_obj(body))
