# Agent Spending Policy — specification, draft 0.1

**Status:** draft for comment; revised after an independent review (`REVIEW-1.md`). **Reference:** `foliant/accounts.py` (Python), `contracts/src/AgentAccounts.sol` (EVM), `foliant-js` (TypeScript). **Vectors:** `vectors.json` in this directory. **Schema:** `spending-policy.schema.json`.

This document specifies a spending policy for agent accounts: a small, chain-agnostic object that bounds what an account may pay, a rule for arranging accounts in a tree so that a crew shares one bound, and the exact evaluation an implementation must perform. It is written so that a wallet, a payment facilitator and a ledger can each evaluate the same policy and agree; the conformance vectors are the test of that agreement.

The policy is independent of how payments are settled. §7 binds it to the x402 `exact` scheme, where every payment is a spend, and to the Foliant channel and pool schemes, where the deposit is the spend. Nothing in §§2–6 depends on either.

## 1. Terms

- **Account.** A payer identity with a `signer` key and a `policy`. Accounts may have a `parent` account; the parent relation forms a tree.
- **Spend.** Value leaving the tree: a transfer to an address outside the tree, or a deposit committed to a channel, pool, escrow or other settlement construct in favour of an outside payee. Moving value between accounts in the same tree is not a spend (§4.3).
- **Payee.** The address that a spend benefits. For a channel or pool deposit it is the channel's payee or the pool's coordinator, not the contract holding the deposit.
- **Window.** The trailing period of `windowSecs` seconds ending at evaluation time, over which spends are summed.
- **Amount.** An unsigned integer in the smallest unit of the asset being spent (see §8.1 on assets).

Requirement words (MUST, SHOULD, MAY) are as in RFC 2119.

## 2. Data model

```json
{
  "perTxMax": "500000000",
  "perWindowMax": "2000000000",
  "windowSecs": 3600,
  "allowList": null,
  "denyList": [],
  "expiry": null,
  "escalation": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `perTxMax` | uint, decimal string | Largest single spend. |
| `perWindowMax` | uint, decimal string | Largest sum of spends in any window. |
| `windowSecs` | uint32, ≥ 1 | Window length in seconds. Implementations MAY bound it above and MUST document the bound: the Python reference bounds it to 30 days; the EVM reference accepts any uint32. |
| `allowList` | array of address, or `null` | If not `null`, the only payees that may be paid. `[]` means no payee. |
| `denyList` | array of address | Payees that may never be paid. Checked before `allowList`. |
| `expiry` | uint64 unix seconds ≥ 1, or `null` | The policy permits nothing at or after this time. `null` means never. `0` is invalid on the wire: a binary encoding MAY use 0 for `null` internally (the EVM reference does), so a wire-form `0` MUST be rejected by the decoder rather than passed through. |
| `escalation` | address or key, or `null` | A co-signer whose signature over a spend lifts `perTxMax` (§5). The all-zero address is invalid for the same reason as `expiry` 0. |

Amounts are decimal strings matching `^(0|[1-9][0-9]*)$` (no sign, no leading zeros, ASCII digits only), so that JSON implementations without 64-bit integers are safe. The wire form sets no upper limit; an implementation MAY bound amounts and MUST document the bound and reject, never truncate, what it cannot represent (both references: 2¹²⁸ − 1, so `policy-006` is schema-valid and rejected by both). A policy that fails any rule in this section is **invalid**, reported as `policy_invalid` at the point it is loaded; it is never evaluated. Implementations MAY also bound tree depth and list length and MUST document those bounds (EVM reference: depth 16, lists of 32; Python reference: none).

**2.1 Addresses.** An address is a string. Two addresses are equal when their canonical forms are equal. For EVM addresses the canonical form is `0x` followed by 40 lowercase hex digits; other chains define their own. Implementations MUST canonicalise every address on input — policy lists when a policy is loaded, and the payee when a spend is evaluated — and compare canonical forms. (Vectors `check-030` and `check-031` give a payee and a list entry in mixed case for this reason.)

**2.2 Canonical encoding and policy id.** The canonical encoding is the JSON object with exactly the seven fields above (a `null` field is present, not omitted), keys sorted bytewise, no whitespace, `perTxMax`/`perWindowMax` as strings, `windowSecs`/`expiry` as integers, addresses in canonical form, `allowList` and `denyList` sorted bytewise on their canonical strings and duplicate-free, and a key-form `escalation` as `{"key": <lowercase hex>, "scheme": <name>}`. The **policy id** is the lowercase hex SHA-256 of that encoding's UTF-8 bytes (vectors of kind `id`). Two policies with the same id are the same policy. Implementations that hash a binary struct instead (the EVM reference) are exempt from this id but MUST provide a deterministic id with the same equality property. The Python reference uses a different internal encoding for its signed envelopes and exposes the specification id separately as `Policy.spec_id`.

## 3. Evaluating a single policy

`check(policy, amount, payee, now, spentInWindow, escalated) → ok | reason`

Inputs: the policy; the spend's `amount` and canonical `payee`; the evaluation time `now`; the sum `spentInWindow` of the account's spends recorded in the window ending at `now` (§6); and whether a valid escalation co-signature accompanies the spend (§5).

The checks MUST be applied in this order, and the first failure is the result. The order matters only for the reason reported; any failure rejects.

| # | Condition | Reason code |
|---|---|---|
| 1 | `amount < 0` (only possible in signed representations) | `negative_amount` |
| 2 | `expiry` is not `null` and `now >= expiry` | `expired` |
| 3 | `payee ∈ denyList` | `payee_denied` |
| 4 | `allowList` is not `null` and `payee ∉ allowList` | `payee_not_allowed` |
| 5 | `amount > perTxMax` and not `escalated` | `per_tx_exceeded` |
| 6 | `spentInWindow + amount > perWindowMax` | `per_window_exceeded` |

Notes. `escalated` MUST be false when `escalation` is `null`; an evaluator handed a co-signature for such a policy rejects it before the policy check, with no §3 code. Expiry is inclusive at the boundary: a policy with `expiry = T` permits nothing at `now = T`. A zero amount passes checks 5 and 6 trivially but is still subject to 2–4; an implementation MAY refuse zero-amount spends before the policy check as a matter of ledger hygiene (the EVM reference does), which is not a policy result and has no reason code here. Escalation never relaxes check 6: the window cap is the ceiling on loss even with the co-signer's cooperation. Arithmetic in check 6 MUST NOT overflow; implementations using fixed-width integers MUST reject rather than wrap. An amount a binary implementation cannot represent (≥ 2¹²⁸ in the EVM reference) is rejected at decode time, like an invalid policy, and is not a policy result; vectors never carry such an amount.

## 4. Trees

**4.1 Delegation.** An account's signer MAY create a child account with its own signer and a policy that is *within* the parent's. `within(child, parent)` fails, with the first applicable reason, when:

| # | Condition | Reason code |
|---|---|---|
| 1 | `child.perTxMax > parent.perTxMax` | `child_per_tx_wider` |
| 2 | `child.perWindowMax > parent.perWindowMax` | `child_per_window_wider` |
| 3 | `parent.allowList` is not `null` and (`child.allowList` is `null` or `child.allowList ⊄ parent.allowList`) | `child_allow_wider` |
| 4 | `parent.denyList ⊄ child.denyList` | `child_deny_narrower` |
| 5 | `parent.expiry` is not `null` and (`child.expiry` is `null` or `child.expiry > parent.expiry`) | `child_expiry_later` |

`windowSecs` and `escalation` are deliberately not compared: a child may meter over a different window than its parent (the parent's window still binds the subtree by 4.2), and a child's co-signer lifts only the child's own `perTxMax` (§5). `within` is checked at delegation and whenever a policy is replaced (`setPolicy`); it is a convenience for administrators, not the safety property. The safety property is 4.2.

**4.2 Every ancestor checks and records.** A spend from account *A* with ancestors *P₁ … Pₙ* (parent first, root last) is evaluated as:

1. For each of *A, P₁, …, Pₙ* in that order: `check(policy, amount, payee, now, spentInWindow_of_that_account, escalated_for_that_account)`. The first failure rejects the spend and nothing is recorded.
2. If all pass: record `amount` at `now` in the window of each of *A, P₁, …, Pₙ*.

`escalated_for_that_account` is true only for *A* itself (§5). The two steps together MUST be atomic: an implementation MUST NOT record in some windows and not others, and MUST NOT let a concurrent spend observe a partially recorded state. This is the property the tree provides — *no subtree ever spends more in any window than any ancestor's `perWindowMax`, whatever its own policies say* — and it holds even if a child holds a policy wider than its parent's (for example after the parent was tightened).

**4.3 Internal moves are not spends.** Funding a child from its parent and recalling a descendant's balance to an ancestor MUST NOT be checked against any policy and MUST NOT be recorded in any window. Only value leaving the tree passes through a policy. An implementation MUST prevent a spend from naming an in-tree account (or the contract that holds tree balances) as its payee, or MUST treat such a payment as an internal move; otherwise a member could launder value out through a sibling.

**4.4 Ancestors administer descendants.** The signer of any ancestor MAY set a descendant's policy, rotate its signer, revoke its escalation and recall its balance, from any depth. Descendants have no authority over ancestors, and an account's own signer MUST NOT change its own policy: a policy is set from above (by an ancestor's signer) or by the account's owner, never by the key it binds. The owner (if the implementation distinguishes one) has the same authority over the whole subtree it owns.

## 5. Escalation

If `escalation` is set, a spend accompanied by a signature from that key over *that exact spend* is evaluated with `escalated = true` for the paying account only. The signature MUST bind, at minimum: the account, the payee, the amount, the asset (where the settlement construct distinguishes assets), a replay guard and a deadline after which it is void. Implementations SHOULD let the signer, the co-signer and any ancestor invalidate an outstanding co-signature before its deadline. The EVM reference uses an EIP-712 message `Escalation(account, token, payee, amount, nonce, deadline)` with a per-account nonce that advances on every use, and any of those three parties may advance the nonce. The Python reference has the co-signer sign the spend envelope itself (which carries the account nonce as the replay guard and an `escalation_deadline`); it has no revoke path other than the account's own signer advancing the nonce, which is a gap against the SHOULD above. Escalation lifts check 5 (`per_tx_exceeded`) only; checks 2, 3, 4 and 6 still apply, and ancestors are never escalated from below.

## 6. Window accounting

**6.1 Exact definition.** A spend recorded at time `t` counts in `spentInWindow(now)` if and only if it has not aged out under any window in force between `t` and `now`: for every `u` with `t < u ≤ now`, `t > u − windowSecs(u)`, where `windowSecs(u)` is the length in force at `u`, and at the instant of a change both the outgoing and the incoming length are in force (so a spend must survive the outgoing length as of the change time). With a fixed window this is the sum over `now − windowSecs < t ≤ now`; a spend stops counting at `t + windowSecs`. The definition depends only on the spend log and the policy history, never on when evaluations happened to run. Because the window is piecewise constant, an implementation obtains it exactly by pruning under the outgoing length *as of the time the change took effect* — not as of the time an evaluator learns of it, which may be later — and under the current length at each evaluation, and never restoring a pruned spend; so shortening drops at once and lengthening never brings back what has already aged out (vectors `window-004`, `window-005`, `window-006`). This is the conservative reading — a longer window can only keep spends, never revive them — and it is what the EVM reference guarantees, since a slot that has aged out may already have been reused.

**6.2 Approximation.** An implementation MAY approximate 6.1 to bound storage or gas, subject to two rules:

- **Never under-count.** For every `now`, the approximated sum MUST be ≥ the exact sum. Under-counting would let more than `perWindowMax` leave in a window.
- **Bounded over-count.** The approximated sum MUST be ≤ the sum of every spend recorded in the last `windowSecs + B` seconds for a documented bound `B`; that is, the implementation may hold a spend for at most `B` seconds longer than 6.1 requires. After a change of `windowSecs` the bound may widen by one further window plus `B` per change (an implementation that does not prune at the change may keep, until the window in force when the slot is next reused, a spend that 6.1 has dropped); this MUST be documented.

The EVM reference uses 32 fixed slots with `B = ⌈windowSecs / 31⌉`; its fuzz test (`WindowFuzz.t.sol`) checks both rules against the exact definition over random spend sequences and window changes. The exact definition is what the Python reference implements, and what the vectors assume.

**6.3 Cost of recording.** Bucketing under 6.2 makes the cost of *recording* a spend depend on when it is recorded: the first spend to land in a bucket initialises that bucket, while a later spend in the same bucket only adds to it. Where recording happens inside a transaction whose resource limit is fixed before the transaction executes — any on-chain implementation — that cost can rise between the moment the limit is set and the moment the transaction runs. A limit taken from a simulation against the state current at the time of the simulation is then too low whenever a bucket boundary falls in between.

This is not an accounting failure: no spend escapes the window and no cap is exceeded. It is a liveness and cost failure, and on the EVM an expensive one, because a transaction that exhausts its gas is mined as a failure that consumed its whole limit. The payer is charged in full and the spend does not happen.

A client that sets a transaction's resource limit from a simulation MUST therefore carry headroom for one bucket initialisation **per account the spend is recorded against** — the account and each of its ancestors (§4.2) — and not for one. A flat allowance is wrong below the root, and silently so: it is sufficient for the root account most implementations test with, and insufficient exactly for the delegated accounts a tree exists to create.

In the EVM reference a bucket is a single storage word (`end` and `amount` are packed), so initialising one costs `SSTORE_SET` where adding to one costs an update: a difference of 17,100 gas (`SSTORE_SET` at 20,000 against a warm `SSTORE_RESET` at 2,900, EIP-2929), and 16,630 measured end to end on the reference's test chain, per account recorded against. Its client carries 40,000 per account, which leaves room for a second, unrelated cliff on the same transactions — paying an ERC-20 payee whose balance is zero when the transaction executes but was not when it was simulated, a further 17,100.

The exposure is bounded and worth stating, because it is small enough to survive testing and large enough to matter in use. With `S` slots and a bucket length of `⌈windowSecs / (S − 1)⌉`, a spend is exposed when a boundary falls between simulation and execution — roughly the time to inclusion divided by the bucket length. On a chain with seconds-level inclusion and a one-hour window that is a few percent of spends; at `windowSecs` of 31 or less the bucket length is one second and it is every spend. Only accounts whose ring still holds unwritten slots are exposed at all: once every slot has been written once, crossing a boundary reuses a slot instead of initialising one. Every newly created account begins in the exposed state and leaves it as it is used.

An implementation MAY remove the cliff rather than cover it, by making the recording cost uniform — writing every slot when the account is created — at the price of charging every account for storage it may never use.

A transaction may also fail after a successful simulation because a §3 condition changed in between: a policy expiry or an escalation deadline passing. That failure leaves resources unspent, and no headroom prevents it. Implementations SHOULD distinguish the two when reporting a failure, since only the first is a defect in the client.

## 7. Bindings

**7.1 Foliant channels and pools.** The policy bounds committed value. Opening a channel or joining a pool is a spend of the deposit, with the channel's payee or the pool's coordinator as payee. Off-chain updates that draw on the deposit are not spends and are not checked: they cannot exceed the deposit, so they cannot exceed the policy. A refund on close or exit returns value to the account and is not "un-spent"; the window keeps the deposit until it ages out. Escalation, if used, is attached to the open or join.

**7.2 x402 `exact`.** In the plain x402 `exact` scheme each payment transfers a fixed amount to `payTo`, so each payment is a spend. This binding covers the EVM payload types, EIP-3009 (`authorization`) and Permit2 (`permit2Authorization`); any other payload type (a Solana transaction, say) is out of scope for draft 0.1 and MUST be reported as `malformed_payment`. The binding is:

| Policy input | From the x402 payment |
|---|---|
| `amount` | v2: `accepted.amount`; v1: `maxAmountRequired`. Amount strings MUST match `^(0|[1-9][0-9]*)$`, else `malformed_payment`. MUST equal the payload's authorised value (`authorization.value` for EIP-3009, `permit2Authorization.permitted.amount` for Permit2); otherwise `amount_mismatch`. |
| `payee` | `payTo`, canonicalised. MUST equal the payload's recipient (`authorization.to` or `permit2Authorization.witness.to`); otherwise `payee_mismatch`. |
| spender (Permit2 only) | `permit2Authorization.spender` MUST be the x402 Permit2 proxy for the network (the SDK deploys one address, `0x402085c2…20001`, on every EVM network it supports); otherwise `spender_mismatch`. A permit naming any other spender grants that contract the tokens whatever `witness.to` says, so the payee would be meaningless. |
| asset | `asset`, canonicalised; MUST equal the Permit2 token where present (`asset_mismatch`). See §8.1. |
| payer | `authorization.from` / `permit2Authorization.from`, canonicalised. An evaluator that knows which account it is evaluating MUST reject a payer that is not that account's payment address (`payer_mismatch`); otherwise a payer could charge another account's window, or escape its own. |
| `now` | The evaluator's clock at evaluation. Not `validAfter`/`validBefore`, which bound the settlement transaction, not the decision. |

Shape errors are `malformed_payment`; a scheme other than `exact` is `unsupported_scheme`; v2 `accepted` disagreeing with separately supplied requirements, or v1 top-level scheme/network disagreeing, is `requirements_mismatch`, where "disagreeing" compares `scheme`, `network`, `asset`, `amount` (v1: `maxAmountRequired`), `payTo` and `extra` (which carries the EIP-712 domain, so it changes what the signature covers) and ignores `maxTimeoutSeconds` and resource metadata. Precedence, first failure reported: `malformed_payment` (version and shape) → `requirements_mismatch` → `unsupported_scheme` → `malformed_payment` (fields and grammar) → `spender_mismatch` → `asset_mismatch` → `payee_mismatch` → `amount_mismatch` → `payer_mismatch` → the §3 codes. A payment that is not a well-formed exact payment is never evaluated against the policy.

Where the check runs:

- *Wallet / signer side* (the account decides whether to sign the authorisation). The wallet holds its own window and its ancestors' policies, or asks a policy service that does. This is where an agent framework's budget middleware belongs. (The Python reference's `AgentSigner` evaluates only its own policy; the ledger enforces the ancestors.)
- *Facilitator side* (the facilitator decides whether to verify/settle). x402 v2 extensions are declared by the resource server in `PaymentRequired.extensions` as `{"<key>": {"info": {…}, "schema": {…}}}` and echoed by the client in `PaymentPayload.extensions` with `info` filled in. This draft reserves the key `spending-policy`, declared by a server whose facilitator supports it, with `info` `{"registry": "<CAIP-2 network>:<contract address or URL of the account registry>"}` from the server and `{"account": "<account id>"}` added by the client; the JSON schema is `spending-policy.extension.schema.json` in this directory. A facilitator that recognises the registry MUST map the payload's payer address to the account (never trust the client's `account` alone; `payer_mismatch` otherwise), evaluate §4.2 against the registry, and return a §3 or §7.2 reason code in `invalidReason` when it rejects. A facilitator that does not recognise the extension ignores it; a server that declared it MAY reject payments that omit it.
- *Ledger side* (settlement itself is policy-checked). This is the Foliant contract path and is not available to plain `exact` settlement, which is why the other two exist.

**7.3 Recording.** Whichever side evaluates, the spend MUST be in its windows if and only if the payment was accepted at that side (signed, or verified): record on acceptance, or record provisionally before releasing the signature or verification and roll back on rejection (the Python reference does the latter); a spend that was merely evaluated MUST NOT remain recorded. Recording SHOULD happen before the signature or verification is released, so a concurrent evaluation sees it.

## 8. Open issues in this draft

**8.1 Assets.** A policy's amounts are nominal: the reference implementations compare `amount` without regard to which asset it is in. A policy is therefore only meaningful for an account that spends one asset, or several with the same decimals and comparable value. Draft 0.2 will add an optional `assets` list with per-asset amounts, or a unit-of-account with an oracle; comments welcome on which.

**8.2 Non-monetary units.** The same object can bound tokens, compute-seconds or API calls if `amount` is read in that unit. The reference implementations do not do this yet.

**8.3 Streams.** A rate-based channel update (`rate × elapsed`) is a spend that grows with time; how the window should account for it is not specified here.

## 9. Security considerations

- The window cap is the loss bound. `perTxMax` limits the size of one mistake; `perWindowMax` limits the total, and nothing lifts it. Set `perWindowMax` from what the operator can afford to lose in a window, not from expected usage.
- Approximate windows over-count, never under-count; an operator sees at most `B` seconds of extra denial, never extra spend.
- A tightened parent binds its descendants from the next spend, without touching them (4.2). A widened child does not widen anything (4.2). On the ledger path (§7.1) these two facts mean a compromised worker's key can lose at most what its ancestors allow, because the policy sits between the key and the value. Under the x402 `exact` binding (§7.2) the policy bounds what a *policy-respecting* signer will authorise: a key used outside its wallet can sign `transferWithAuthorization` directly, the facilitator-side check is opt-in per server, and anyone holding the signature can settle it. The ledger path is the only one where the bound holds against a compromised key.
- The window bounds *authorised* value by authorisation time. Settlement of an `exact` payment may lag by up to its validity (an hour by SDK default), so the value actually leaving in any wall-clock interval of `windowSecs` can reach twice `perWindowMax`.
- Escalation signatures MUST be bound to a specific spend, nonce and deadline; an escalation that merely says "allow more" is a second key with no bound.
- Internal moves being unchecked (4.3) is safe only because value still has to leave through a policy; an implementation that lets an in-tree account be named as an outside payee breaks it.
- Clock: `now` is the evaluator's clock. A ledger uses block time; a wallet or facilitator uses its own and SHOULD tolerate skew by treating `expiry` conservatively (rejecting slightly early is safe; accepting late is not).

## 10. Conformance

An implementation conforms to this draft if it passes every vector in `vectors.json` whose `kind` it implements: `policy` (§2 validity), `id` (§2.2), `check` (§3), `within` (§4.1), `window` (§6.1, or §6.2 with the implementation's bound), `tree` (§4.2–4.4) and `x402-exact` (§7.2). The vector file format is described at its head. The Python reference runs all seven kinds (`tests/test_spec_vectors.py`). The EVM reference runs `check`, `within`, `window`, `tree` and `policy` (`contracts/test/SpecVectors.t.sol`) and skips `id` (it hashes a struct) and `x402-exact` (no on-chain counterpart), with the departures the spec permits handled in the runner and stated in its header: windows are checked against the §6.2 bounds rather than the exact figure and the `exactOnly` vectors are skipped; the three zero-amount `check` vectors are skipped because the contract refuses zero spends before the policy (§3 note); `check-030` is skipped because addresses are bytes on-chain; `policy-003` and `policy-009` (wire-form `expiry` 0 and the zero-address co-signer, the contract's own null encodings) are the decoder's rejection, not the contract's; a tree `fund` op is applied at the child's delegation, the only point the contract funds a child; and the escalation co-signer in a vector is replaced by a key the runner holds so that `escalated` is a real co-signature. An implementation that skips a vector MUST say why, as here. The requirement in 6.3 is not covered by any vector: it concerns what a client does with a transaction's resource limit, which the vectors do not model, so it is a requirement on implementations rather than a conformance test. The TypeScript client (`foliant-js`) implements §3 for its own signing decisions and does not yet run the vectors or canonicalise addresses; it is not a conforming implementation of this draft.

## 11. Relation to other work

Within x402 itself: the `upto` scheme bounds a single authorisation (spend up to X; the facilitator settles no more than that) and the `batch-settlement` scheme settles many payments from a deposit with vouchers and claims, which is the deposit-is-the-spend model of §7.1 rather than per-payment spends. Neither carries a per-account policy, a window or a tree; both could sit under one (a `batch-settlement` deposit is a spend under §7.1; an `upto` authorisation's cap could be the spend under a §7.2-style binding, which draft 0.1 does not define — `upto` payments are `unsupported_scheme` here). Smart-wallet session keys and spend permissions bound one key on one chain, and can be the signer of an account under this policy. The whitepaper's prior-art section (§11a) covers the wider field with sources; this draft makes no further claims about other systems.
