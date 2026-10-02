"""Every policy-kind vector, checked against docs/spec/spending-policy.schema.json.

The schema is the only machine-readable part of the specification, and until this test nothing in
the repository validated anything against it. That is how the loaders came to drop unknown fields
while the schema had said `additionalProperties: false` all along: the two could disagree
indefinitely and no test would notice.

Both directions are checked, because only one of them tests the schema. A vector the spec calls
valid must validate -- that tests the schema is not too tight. A vector the spec calls invalid must
*fail* to validate -- that tests it is tight enough. Where a rule is one JSON Schema cannot
express, the vector is named in SCHEMA_CANNOT_EXPRESS with the reason, and a separate test proves
the reference rejects it instead, so nothing falls through both.
"""
import json
from pathlib import Path

import jsonschema
import pytest

from foliant.accounts import Policy
from foliant.errors import PolicyViolation

SPEC = Path(__file__).resolve().parent.parent / "docs" / "spec"
SCHEMA = json.loads((SPEC / "spending-policy.schema.json").read_text())
VECTORS = [v for v in json.loads((SPEC / "vectors.json").read_text())["vectors"]
           if v["kind"] == "policy"]
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)

# Rules §2 states that JSON Schema cannot carry. Each is written in the schema's own `description`
# as a requirement on decoders rather than a constraint, so the schema is not wrong -- it is as
# tight as the format allows. The reference enforces both; test_reference_rejects_* proves it.
SCHEMA_CANNOT_EXPRESS = {
    "policy-006": "2^128 as a decimal string is well formed. 'too large for the reference "
                  "encodings' is a numeric bound on a string, which $defs.uint's pattern cannot "
                  "express; its description states it as a requirement on decoders.",
    "policy-009": "$defs.address is any non-empty string, because non-EVM chains define their own "
                  "canonical form. 'never the zero address as an escalation co-signer' is in its "
                  "description, not in a pattern.",
}


def _ids(*expect: str) -> list[str]:
    return [v["id"] for v in VECTORS if v["expect"] in expect]


def _vector(vid: str) -> dict:
    return next(v for v in VECTORS if v["id"] == vid)


def test_there_are_vectors_to_check():
    """A path typo or a renamed `kind` would otherwise make every test below vacuously pass."""
    assert len(VECTORS) >= 9
    assert _ids("ok") and _ids("policy_invalid")


@pytest.mark.parametrize("vid", _ids("ok"))
def test_valid_vectors_validate(vid):
    """The schema must not reject a policy the spec calls valid."""
    VALIDATOR.validate(_vector(vid)["policy"])


@pytest.mark.parametrize("vid", _ids("policy_invalid"))
def test_invalid_vectors_fail_validation(vid):
    """The schema must reject a policy the spec calls invalid, or say why it cannot."""
    errors = list(VALIDATOR.iter_errors(_vector(vid)["policy"]))
    if vid in SCHEMA_CANNOT_EXPRESS:
        assert not errors, (
            f"{vid} is listed in SCHEMA_CANNOT_EXPRESS but the schema now rejects it "
            f"({errors[0].message}). The schema got tighter: remove the entry."
        )
    else:
        assert errors, f"{vid} is invalid per the spec but the schema accepts it"


@pytest.mark.parametrize("vid", sorted(SCHEMA_CANNOT_EXPRESS))
def test_reference_rejects_what_the_schema_cannot(vid):
    """The two halves have to cover the whole: whatever the schema lets through, the code stops."""
    assert vid in _ids("policy_invalid"), f"{vid} is listed but is no longer an invalid vector"
    with pytest.raises(PolicyViolation):
        Policy.from_wire(_vector(vid)["policy"])


@pytest.mark.parametrize("vid", _ids("ok"))
def test_wire_form_round_trips_through_the_schema(vid):
    """What the reference emits must validate too, not just what the vector file holds -- otherwise
    canonicalisation (§2.1 sorting, address lowercasing) could drift out of the schema unnoticed."""
    VALIDATOR.validate(Policy.from_wire(_vector(vid)["policy"]).wire())


def test_the_policy_object_forbids_unknown_fields():
    """The rule the loaders were fixed to honour, asserted against the schema that always said it,
    at both levels the schema sets `additionalProperties: false`."""
    good = _vector("policy-001")["policy"]
    assert list(VALIDATOR.iter_errors({**good, "perAssetMax": {"USDC": "1"}}))
    nested = {**good, "escalation": {"scheme": "ed25519", "key": "ab", "extra": 1}}
    assert list(VALIDATOR.iter_errors(nested))
