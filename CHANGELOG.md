# Changelog

`foliant-protocol`, the Python reference implementation. Versions follow
[semantic versioning](https://semver.org) with the usual 0.x caveat: the public API may still
change. Where a release changes behaviour an existing caller could depend on, this file says so
in the entry rather than only in the version number.

## 0.1.5 — 2026-10-02

### Fixed

- **Key references from the wire are validated.** `PublicKey.from_dict` took `scheme` and `key`
  and checked neither. Every owner, signer and escalation co-signer key in the ledger enters
  through it, so this was the widest of the loader holes rather than a policy detail:

  - bad hex raised `ValueError` and a missing `key` raised `KeyError`. Neither is a `FoliantError`,
    so `foliant/node.py` let them out as **HTTP 500**. Four ingress paths answered 500 and now
    answer 400.
  - an **unimplemented scheme** reached `verify`, which raised `ValueError` — a fifth 500, on
    `/ledger/tx`. It now raises `InvalidKey`, so the node answers 400. Signature agility is
    unchanged: such a key still parses, because its length cannot be known, and `verify` is still
    where it is refused.
  - an unknown nested field was dropped although the schema's `$defs.keyRef` sets
    `additionalProperties: false` — 0.1.3's defect, one level down.
  - **a two-byte string was accepted as an ed25519 public key.** This is the one that mattered.
    Registration caught it by accident, because truncating the key changes the policy id and the
    owner's signature stops matching, but `set_policy` carries the policy inside an envelope the
    owner has already signed, so nothing cross-checked it. An account could be left holding an
    escalation co-signer that cannot verify anything: the owner believes a co-signer can lift
    `perTxMax` when no signature it produces will ever be accepted. Demonstrated end to end
    against 0.1.4 and refused now.

  A key is now exactly `{"scheme", "key"}`, both present, `key` a non-empty even-length lowercase
  hex string (§2.2 specifies lowercase), and the right length for its scheme where the scheme is
  one this implementation knows — 32 bytes for ed25519. This is stricter than the schema in one
  way the schema cannot express: odd-length hex matches `^[0-9a-f]+$` but is not a whole number of
  bytes.

  `foliant/crypto.py` now imports `InvalidKey` from `foliant/errors.py`, where it previously raised
  `ValueError` and depended on nothing. That dependency is the point: these are rejections of
  untrusted input, and the node's contract is that a `FoliantError` is a 400. The alternative — a
  wider `except` in `node.py` — would have turned genuine bugs into 400s too. Inside a policy the
  failure is translated to `PolicyViolation` with code `policy_invalid`, because §2 requires that
  code whichever part of a policy is invalid.

### Known issues

- `foliant-client` does not mirror these key rules. It is no longer a soundness gap now that the
  node refuses malformed keys, but the two implementations still disagree in one place: an
  uppercase-hex key reference is refused here and silently canonicalised there, which gives the
  same policy two ids.
- Unchanged from 0.1.4: no conformance vector for the unknown-field rule (deferred to draft 0.2),
  `windowSecs` bounded per-implementation rather than by the specification, and `foliant-client`
  carrying amounts as JavaScript numbers.

## 0.1.4 — 2026-10-02

### Added

- **The specification's JSON Schema is now checked by the test suite**, in both directions
  (`tests/test_schema.py`). Every `policy`-kind conformance vector the spec calls valid must
  validate against `docs/spec/spending-policy.schema.json`, and every vector it calls invalid must
  fail to validate. Nothing in the repository had ever validated anything against the schema,
  which is how 0.1.3's bug survived: the schema said `additionalProperties: false` and the loaders
  disagreed, indefinitely and silently.

  Two of the five invalid vectors turn on rules JSON Schema cannot carry, and both are already
  written in the schema's own `description` fields as requirements on decoders: `policy-006`,
  because "2^128 is too large" is a numeric bound on a decimal string that `$defs.uint`'s pattern
  cannot express, and `policy-009`, because `$defs.address` is any non-empty string so that
  non-EVM chains can define their own canonical form, leaving "never the zero address as an
  escalation co-signer" to prose. They are named in `SCHEMA_CANNOT_EXPRESS` with those reasons,
  and a separate test proves `from_wire` rejects both — so nothing falls through the gap between
  the schema and the code. Tightening the schema fails the test with a message saying to shorten
  that list.

  `jsonschema` is added to `requirements.txt` and the `test` extra. It is test-only; nothing in
  the published package imports it.

### Fixed

- **A policy missing one of the seven fields is now invalid** rather than taking this reference's
  default. This is the other half of 0.1.3's fix and the more dangerous half: the defaults are the
  permissive readings — no expiry, no allow list — so a field left out widened authority, and
  `from_dict` is reached from inside a *signed* envelope (`_op_set_policy`, `_op_delegate`), which
  means the signature covered the omission. `_reject_unknown` becomes `_check_fields` and now
  rejects both extra and missing keys.
- **`expiry` is bounded above by 2^64 - 1**, which the schema already stated and the code did not.
- **An address-form escalation co-signer works in the envelope encoding.** `wire()` and
  `from_wire` handled all three forms the spec allows (keyRef object, address string, null);
  `to_dict` and `from_dict` assumed a keyRef, so an address-form co-signer raised `TypeError` on
  load and `AttributeError` from `Policy.id` and `to_dict`. Both loaders now share one
  `_escalation` helper so the two encodings cannot disagree about which forms exist.
- **An address list that is not a list, or holds a non-string, or holds duplicates, is now
  invalid.** A bare string where a list belonged was silently accepted and became a set of single
  characters; a non-string entry raised `AttributeError` out of `canonical_address`; duplicates
  were deduplicated although the schema sets `uniqueItems: true`. Two spellings of one EVM address
  still collapse, deliberately: the schema permits them, since `uniqueItems` compares the strings
  as given, and §2.1 defines addresses to compare in lowercase.
- **All three of 0.1.3's ingress 500s are now 400s.** `POST /ledger/accounts` answers
  `policy_invalid` for a missing field, an address-form co-signer and a non-string list entry,
  rather than `Internal Server Error`. This came out of the loaders rather than a wider `except`
  in `foliant/node.py`, which would have turned genuine bugs into 400s too.
- **`demo/run_uses.py` runs again.** Scenario 5 built its tree with `window_secs` of 365 and 90
  days against a `MAX_WINDOW_SECS` of 30, so it had been failing before it reached its first
  assertion. The scenario had conflated how long a delegation's authority lasts (`expiry`) with
  the period its rate limit covers (`window_secs`); the expiries are unchanged and the windows are
  now 30, 14 and 7 days, still nested.

### Known issues

- **The escalation `keyRef` object is unvalidated, and it is worse than 0.1.3 recorded.**
  `PublicKey.from_dict` takes `scheme` and `key` and checks neither: a malformed hex key raises
  `ValueError` and a missing `key` raises `KeyError` — both 500s at ingress — an unknown nested
  field is dropped although the schema forbids it, and a two-byte string is accepted as an ed25519
  public key. It is deferred rather than bundled here because `PublicKey.from_dict` parses every
  owner and signer key in the ledger, not only co-signers, so a wrong-length *signer* key sails
  through the same gap. That blast radius deserves its own pass.
- `windowSecs` is bounded at 30 days by this reference and by nothing in the EVM reference, which
  accepts any uint32. §2 and §6.2 permit an implementation to set its own bound and declare it, so
  this conforms — but it means a policy that is legal on-chain is refused by this node, which is
  the same disease as a dropped field one level up: a policy's validity depends on who parses it.
  Whether `windowSecs` should have one bound in the specification is a question for draft 0.2.
- There is still no conformance vector for the unknown-field rule. Adding one needs a §10 edit, so
  it is deferred to the draft 0.2 work rather than opening the specification twice.
- `foliant-client` carries amounts as JavaScript numbers and is correct only for assets with up to
  6 decimals; see that package's README. A `bigint` migration is tracked for its 0.2.0.

## 0.1.3 — 2026-10-02

**Never released.** The fix below turned out to be incomplete, and 0.1.4 went out the same day
instead, so there is no 0.1.3 on PyPI and no `v0.1.3` tag. The entry stays because 0.1.4's entry
refers back to what this version changed, and because the sequence is the point: a loader hole
closed in one place and still open one level down.

### Fixed

- **`Policy.from_dict` and `Policy.from_wire` now reject a policy carrying a field the
  implementation does not implement**, raising `PolicyViolation` with code `policy_invalid`,
  instead of silently discarding it.

  Previously the unknown field was dropped and `Policy.id` was then computed over what survived,
  so the policy the owner signed was not the policy that got enforced. An owner who wrote a
  per-asset cap of 1 USDC into a policy, signed it, and registered it against an implementation
  that did not implement per-asset caps got an account with no such cap and a signature that
  appeared to cover one. The specification already required refusal rather than tolerance —
  `docs/spec/spending-policy.schema.json` sets `additionalProperties: false` on the policy object
  and on `$defs.keyRef` — so this release makes the loaders agree with the schema.

  **This is a breaking change in practice despite the patch version.** `POST /ledger/accounts`
  now answers 400 where it answered 200. If you were sending fields beyond the seven
  (`perTxMax`, `perWindowMax`, `windowSecs`, `allowList`, `denyList`, `expiry`, `escalation`;
  `per_tx_max` … in the envelope encoding), remove them, or use an implementation that implements
  them. The patch version is deliberate: 0.2.0 is reserved for the release that implements spec
  draft 0.2, so that the SDK minor and the spec draft stay legible against each other.

- A non-object passed to either loader now raises `PolicyViolation` rather than `TypeError`.

### Known issues

- Three malformed-policy shapes still reach `POST /ledger/accounts` as HTTP 500 rather than 400,
  because `foliant/node.py` catches only `FoliantError`: a missing required field (`KeyError`), an
  address-form `escalation` (`TypeError`), and a non-string entry in `allowList` (`AttributeError`).
- `expiry` is not bounded to uint64 at load.
- The nested `keyRef` object inside `escalation` does not yet reject unknown fields, although the
  schema forbids them there too. It is the same defect one level down.
- Nothing in the repository validates a policy against `spending-policy.schema.json`, and there is
  no conformance vector for the unknown-field rule, so a third implementation can get this wrong
  and still pass the suite. Adding the vector requires a specification edit (§10 would need a
  skip), so it is deferred to the draft 0.2 work rather than slipped in here.
- `demo/run_uses.py` fails independently of this release: it builds a policy with
  `window_secs=365*day`, which exceeds `MAX_WINDOW_SECS`.

## 0.1.2 — 2026-10-01

### Fixed

- Transactions that record a spend against the account and every ancestor are now submitted with
  gas headroom over `eth_estimateGas` (`GAS_LIMIT_HEADROOM`, per level of the account tree).
  Estimation runs against current state while execution happens later, so a send estimated just
  before a spend-window bucket rolls over — or before the payee's token balance goes from zero —
  was estimated too low and reverted out of gas. The headroom covers the measured worst case of
  both: 16,630 gas for a bucket rollover and 17,100 for a zero-balance payee, per account
  recorded. Failure messages now distinguish "ran out of gas" from "reverted".

## 0.1.1 — 2026-09-29

### Changed

- Pool updates carry the claim epoch, so a stale update cannot be replayed against a later
  membership.
- Channel and pool updates must be balance-ordered.
- The agent signer snapshots and rolls back its spend window when the ledger refuses a
  transaction, so a spend that never happened is not counted against the policy.

## 0.1.0 — 2026-09-28

First release of the reference implementation: agent accounts with hierarchical spending policies,
payment channels, pooled settlement, and HTTP 402 metering in the x402 wire format.
