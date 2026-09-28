"""Keys, signatures and canonical hashing.

Ed25519 stands in for the production scheme. Whitepaper principle 9 requires
signature agility, so every signed message carries a `scheme` tag and the
verifier dispatches on it; only "ed25519" is implemented here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

SCHEME = "ed25519"


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
            raise ValueError(f"unsupported scheme {self.scheme}")
        try:
            Ed25519PublicKey.from_public_bytes(self.raw).verify(signature, message)
            return True
        except InvalidSignature:
            return False

    def to_dict(self) -> dict:
        return {"scheme": self.scheme, "key": self.hex}

    @classmethod
    def from_dict(cls, d: dict) -> "PublicKey":
        return cls(d["scheme"], bytes.fromhex(d["key"]))


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
