"""Key references from the wire: every owner, signer and escalation co-signer enters through
`PublicKey.from_dict`.

Before these checks existed, bad hex and a missing `key` raised ValueError and KeyError -- neither
a FoliantError, so `foliant/node.py` let them out as HTTP 500 -- an unknown nested field was
dropped although the schema forbids it, and a two-byte string was accepted as an ed25519 public
key. The last is the one that matters: an account could be left holding a co-signer that cannot
verify anything, so the owner believes an escalation path exists where none does.
"""
import pytest

from foliant import KeyPair, Ledger, Policy, sign
from foliant.crypto import KEY_BYTES, SCHEME, PublicKey
from foliant.errors import FoliantError, InvalidKey, PolicyViolation

GOOD = KeyPair.from_seed(b"co-signer").public


@pytest.mark.parametrize("bad,match", [
    ({"scheme": SCHEME, "key": "zz"},                      "lowercase hex"),   # was ValueError -> 500
    ({"scheme": SCHEME},                                    "missing key reference"),  # was KeyError -> 500
    ({"key": "ab" * 32},                                    "missing key reference"),
    ({"scheme": SCHEME, "key": "ab" * 32, "extra": 1},      "unknown key reference"),  # schema forbids
    ({"scheme": SCHEME, "key": "abcd"},                     "is 32 bytes, not 2"),     # was accepted
    ({"scheme": SCHEME, "key": "abc"},                      "lowercase hex"),   # odd length is not bytes
    ({"scheme": SCHEME, "key": "AB" * 32},                  "lowercase hex"),   # §2.2 says lowercase
    ({"scheme": SCHEME, "key": ""},                         "lowercase hex"),
    ({"scheme": SCHEME, "key": 1},                          "lowercase hex"),
    ({"scheme": "", "key": "ab" * 32},                      "non-empty string"),
    ({"scheme": 7, "key": "ab" * 32},                       "non-empty string"),
])
def test_from_dict_refuses(bad, match):
    with pytest.raises(InvalidKey, match=match):
        PublicKey.from_dict(bad)


@pytest.mark.parametrize("bad", ["abc", 7, None, [{"scheme": SCHEME, "key": "ab" * 32}]])
def test_from_dict_refuses_a_non_object(bad):
    with pytest.raises(InvalidKey, match="must be an object"):
        PublicKey.from_dict(bad)


def test_a_valid_key_reference_still_round_trips():
    assert PublicKey.from_dict(GOOD.to_dict()) == GOOD
    assert len(GOOD.raw) == KEY_BYTES[SCHEME]


def test_an_unimplemented_scheme_parses_but_cannot_verify():
    """Signature agility (crypto.py's docstring): an envelope may name a scheme this node does not
    implement. Its length cannot be checked, so it parses; `verify` is where it is refused, and it
    raises a FoliantError so the node answers 400 rather than 500."""
    other = PublicKey.from_dict({"scheme": "ed448", "key": "ab" * 57})
    assert other.scheme == "ed448"
    with pytest.raises(InvalidKey, match="unsupported scheme ed448"):
        other.verify(b"message", b"signature")
    assert issubclass(InvalidKey, FoliantError)      # the reason it is a 400 and not a 500


def test_a_policy_with_a_bad_co_signer_is_policy_invalid():
    """§2: an invalid policy reports policy_invalid whichever part of it is invalid, so the
    key-level failure is translated rather than leaking out of the policy loader."""
    base = Policy(per_tx_max=1, per_window_max=1, window_secs=60).to_dict()
    with pytest.raises(PolicyViolation, match="escalation: a ed25519 key is 32 bytes") as e:
        Policy.from_dict({**base, "escalation": {"scheme": SCHEME, "key": "abcd"}})
    assert e.value.code == "policy_invalid"


def test_set_policy_cannot_store_an_unusable_co_signer():
    """The path that had no accidental guard. Registration happens to catch a malformed co-signer,
    because dropping or truncating it changes the policy id and the owner's signature stops
    matching; set_policy carries the policy inside an envelope the owner has already signed, so
    nothing cross-checked it and the account was left with a co-signer that verifies nothing."""
    L = Ledger()
    owner, signer = KeyPair.from_seed(b"o"), KeyPair.from_seed(b"s")
    pol = Policy(per_tx_max=10, per_window_max=100, window_secs=60)
    acct = L.register_account(
        sign(owner, {"op": "register", "signer": signer.public.to_dict(),
                     "policy_id": pol.id, "salt": 0}),
        signer=signer.public, policy=pol, salt=0)
    for esc in ({"scheme": SCHEME, "key": "abcd"},
                {"scheme": SCHEME, "key": "ab" * 32, "extra": "dropped"}):
        env = sign(owner, {"account": acct.id, "nonce": L.accounts[acct.id].nonce,
                           "op": "set_policy", "policy": {**pol.to_dict(), "escalation": esc}})
        with pytest.raises(PolicyViolation):
            L.apply(env)
    assert L.accounts[acct.id].policy.escalation is None   # unchanged by the refused updates
