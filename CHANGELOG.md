# Changelog

`foliant-protocol`, the Python reference implementation. Versions follow
[semantic versioning](https://semver.org) with the usual 0.x caveat: the public API may still
change. Where a release changes behaviour an existing caller could depend on, this file says so
in the entry rather than only in the version number.

## 0.1.3 — 2026-10-02

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
