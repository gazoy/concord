# REVIEW-1 — Agent Spending Policy draft 0.1

Independent review of `spending-policy.md`, `spending-policy.schema.json`, `vectors.json`, `foliant/policy_x402.py`, the Python reference (`foliant/accounts.py`, `foliant/ledger.py`), the EVM reference (`contracts/src/AgentAccounts.sol`) and the two vector runners, checked against the x402 Python SDK (`schemas/payments.py`, `schemas/v1.py`, `mechanisms/evm/types.py`, `mechanisms/evm/exact/facilitator.py`, `mechanisms/evm/exact/v1/facilitator.py`, `mechanisms/evm/exact/permit2_utils.py`, `mechanisms/evm/constants.py`, `extensions/*`).

Reproductions: `scratchpad/test_audit_spec.py` (11 pytest cases, all pass = all defects confirmed) and `contracts/test/AuditSpec.t.sol` (5 forge tests, all pass). Both official runners pass as shipped: `python3 -m pytest tests/test_spec_vectors.py -q` → 91 passed; `FOUNDRY_PROFILE=session forge test --match-contract SpecVectors` → 59 run, 6 skipped, PASS.

## Summary

The core of the draft is sound: `check` (§3) and `within` (§4.1) are implemented in the stated order by both references, the every-ancestor check-then-record walk (§4.2) is atomic in both, escalation lifts only the paying account's `perTxMax` in both, refunds/recalls never un-spend, delegation after a full parent window is still bound, and the uint128 arithmetic on-chain cannot wrap. I could not construct a way for a subtree to exceed an ancestor's `perWindowMax` on the ledger path. Two things break the "two implementations agree" claim, however: (1) the `windowSecs`-change rule in §6.1 ("not resurrected") is not a function of the spend log — it depends on *when the window was last evaluated*, so the Python reference gives different `spentInWindow` for identical inputs, the wallet and ledger sides of the same Python system can disagree on allow/deny, and the EVM reference does the opposite (it resurrects); (2) `expiry = 0` is a legal value in the schema and means "always expired" in Python but "never expires" on-chain, in both `check` and `within`. Beyond those, the spec makes several statements about the references that are false (30-day window bound; Python's policy id, canonicalisation, escalation deadline and `windowSecs ≥ 1` validation), the x402 binding does not check the Permit2 `spender` (an allow-list bypass for a wallet-side evaluator), the proposed `extensions["spending-policy"]` shape does not follow the x402 v2 extension envelope, and §9 overstates what the x402 binding can guarantee against a compromised signer. All 91 vectors' expected values are correct as written; the runners are honest except for two soft spots noted in S-14/S-15. Coverage gaps are listed at the end.

## Findings

### S-1 (High) — §6.1 window-change rule is evaluation-history dependent; references disagree with the text and with each other

**Location.** `spending-policy.md` §6.1 last sentence; `foliant/accounts.py:146-149` (`SpendWindow.spent` prunes destructively); `contracts/src/AgentAccounts.sol:450-456, 463-481`; `contracts/test/WindowFuzz.t.sol:_exact`; vector `window-004`.

**Description.** §6.1 defines `spentInWindow(now)` as a sum over the log, then adds "spends already dropped under a shorter window are not resurrected by a longer one". "Dropped" is not an event in the exact definition; in the Python reference it is the side effect of calling `spent()`. So for the same records, the same `now` and the same current `windowSecs`, the answer depends on whether any evaluation happened between a spend aging out and the window being lengthened. The ledger only evaluates on a spend (`ledger.py:134`), `set_policy` does not evaluate, and the wallet side (`AgentSigner`) evaluates on its own schedule (including `sign_update`, `accounts.py:224`), so the two windows in one Python deployment diverge. The EVM reference simply counts any slot whose `end > now − windowSecs` under the *current* window (`AgentAccounts.sol:454`), i.e. it *does* resurrect (it is allowed to by §6.2, which only says ≥ exact). `WindowFuzz.t.sol` encodes the Python pruning behaviour as "exact", so the fuzz test's oracle inherits the same history dependence. `window-004` passes only because an `expect` op at t=4700 happens to precede the `setWindow`; remove that op and the Python reference returns 200, not 100.

**Evidence.** `test_S1_spentInWindow_depends_on_evaluation_history` (100 vs 200 for identical inputs); `test_S1_ledger_and_wallet_disagree_on_allow_deny_after_window_change` (wallet window 0, ledger window 100, ledger rejects with `per_window_exceeded` a spend the wallet signed); `AuditSpecTest.test_S1_lengthening_window_resurrects_aged_out_spend` (on-chain 0 → 100 after `setPolicy`).

**Fix.** Make 6.1 a pure function of the log and the current policy: "`spentInWindow(now) = Σ amount over spends recorded at t with now − windowSecs(now) < t ≤ now`, where `windowSecs(now)` is the policy's window length at evaluation time. A change of `windowSecs` therefore applies immediately to all recorded spends, including ones that were outside the previous window." Delete the "not resurrected" sentence. That is the conservative direction (lengthening can only add), matches the EVM reference, and removes the state dependence. In Python, stop `SpendWindow.spent` from pruning by the current window (prune only entries older than, say, `2^32` s, or the largest `windowSecs` the account has ever held). Update `window-004`: after `setWindow 86400` at 4700 expect `200`; at 5700 expect `300`; at 88400 expect `100` (unchanged). Rewrite `WindowFuzz._exact` to the pure definition. If the authors instead want the "dropped for good" semantics, it must be defined as a rule on the log (e.g. "a spend recorded at t under window W_rec is never counted at any now ≥ t + W_rec"), stated as a MUST, implemented on both sides, and the EVM header/§6.2 bound rewritten — but note that rule also disagrees with the current Python behaviour when the window is lengthened *before* the spend ages out.

### S-2 (High) — `expiry = 0` (and the zero address) mean opposite things in the two references

**Location.** §2 table (`expiry`: "uint64 unix seconds, or null"); `schema.json:20-25` (`minimum: 0`); `accounts.py:75` (`now >= 0` → always expired); `AgentAccounts.sol:51, 485, 506` (`0 = never`).

**Description.** A policy document `{"expiry": 0, …}` validates against the schema. Python `check` rejects every spend with `expired`; the contract accepts forever. `within` disagrees the same way: Python treats a parent with `expiry: 0` as expiring (so a child with `null` is `child_expiry_later`), the contract treats it as never expiring (ok). The same encoding collision exists for `escalation`: `"0x000…0"` is a valid address string but means "none" on-chain; harmless in practice (nobody can sign for it) but two `id`s for one policy.

**Evidence.** `test_S2_python_expiry_zero_means_always_expired`, `test_S2_within_disagrees_on_expiry_zero`, `AuditSpecTest.test_S2_expiry_zero_is_never_on_chain`.

**Fix.** In §2: "`expiry` MUST be `null` or an integer ≥ 1; `0` is invalid. A binary encoding MAY represent `null` as 0 internally but MUST reject a policy whose wire-form `expiry` is 0." Schema: `"minimum": 1`. Same sentence for `escalation` and the zero address. Add `check`/`within` vectors with `expiry: 0` expecting a rejection at load, or a `policy-invalid` kind.

### S-3 (Medium) — §2 says the EVM reference bounds `windowSecs` to 30 days; it does not

**Location.** §2 table row `windowSecs`; `AgentAccounts.sol:509-512` (`_validatePolicy` only rejects 0). The only `30 days` constants are `Pools.sol:60` and `PaymentChannels.sol:47` (`MAX_TIMEOUT`, unrelated).

**Evidence.** `AuditSpecTest.test_S3_no_30_day_bound_on_windowSecs` registers `windowSecs = 2^32 − 1`.

**Fix.** Either add `if (p.windowSecs > 30 days) revert BadPolicy("window too long")` to `_validatePolicy`, or change the sentence to "the EVM reference accepts any uint32". (`WindowFuzz` bounds its inputs to 30 days, which may be where the claim came from.)

### S-4 (Medium) — Python policy id is not the §2.2 id; §2.2 is under-specified

**Location.** §2.2; `accounts.py:36-49` (`to_dict` uses snake_case keys, integer amounts, non-canonicalised lists); `crypto.py:24-26`.

**Description.** §2.2 says the id is SHA-256 of the canonical JSON of "the fields above" (camelCase, decimal-string amounts). The Python reference hashes `{"per_tx_max": 500, …}`. Only a binary-struct hasher (the EVM reference) is exempted. §2.2 also does not say: the sort order of addresses (bytewise on the canonical string, presumably); whether `windowSecs`/`expiry` are JSON integers or strings; how a `keyRef` escalation is encoded (keys sorted? `raw` hex or base64?); whether `null` fields are present or omitted. There is no vector of kind `id`.

**Evidence.** `test_S4_python_policy_id_is_not_spec_id`.

**Fix.** Make `Policy.to_dict()` emit the wire form (`perTxMax` as decimal string, canonicalised sorted lists) and derive `id` from it; keep `from_dict` accepting both during migration. In §2.2 state: fields in camelCase; `perTxMax`/`perWindowMax` strings; `windowSecs`/`expiry` integers; `null` fields present; lists sorted bytewise on canonical strings; keyRef encoded as `{"key":…,"scheme":…}`. Add 2–3 `id` vectors (policy document → hex id).

### S-5 (Medium) — Canonicalisation: MUST in the spec, done by the test runner instead of the Python reference; stored lists are only SHOULD

**Location.** §2.1; `accounts.py:57-58, 77-79` (no lowercasing in `from_dict` or `check`); `ledger.py:130-138` (`payee` used as given); `tests/test_spec_vectors.py:39-40, 61` (runner lowercases lists and payee); vector `check-030`.

**Description.** The spec says implementations MUST canonicalise before comparing. `Policy.check` compares raw strings, and `check-030` passes only because the runner lowercases the payee before calling it. `from_dict` stores allow/deny entries as given, so a canonicalised payee against a mixed-case stored entry misses the deny list. §2.1 says stored lists SHOULD be canonicalised — that is too weak: with a canonicalised input and a non-canonical stored list, `payee ∈ denyList` is false and two implementations disagree on allow/deny.

**Evidence.** `test_S5_python_check_is_case_sensitive` (`payee_not_allowed` for `0xAAAA…` against an allow list holding `0xaaaa…`).

**Fix.** §2.1: "Implementations MUST canonicalise addresses on input (policy lists and payee) and compare canonical forms." Python: canonicalise in `Policy.__post_init__`/`from_dict` and in `check` (or in `Ledger._authorise` and `policy_of`), and stop lowercasing in the runner so `check-030` tests the reference. Add a vector whose *list* entry is mixed-case.

### S-6 (Medium) — §5 escalation: the Python reference binds no deadline and has no revoke path; the spec describes only the EVM reference as "the reference"

**Location.** §5; `ledger.py:163-169` (co-signature must equal the envelope body — account, account-nonce, op, to, asset, amount — no deadline); `AgentAccounts.sol:340-357, 405-422`.

**Description.** §5 says the signature MUST bind "a replay guard and a deadline" and that "the reference … lets the signer, the co-signer and any ancestor invalidate outstanding signatures by advancing the nonce". In Python there is no deadline, the message is not `Escalation(account, token, payee, amount, nonce, deadline)`, and only the account's own signer can burn the nonce (by submitting any op); the co-signer and ancestors cannot revoke. The Python reference violates a MUST in its own spec.

**Evidence.** `test_S6_python_escalation_signature_has_no_deadline` — a co-signature is accepted ten years after it was made.

**Fix.** Either add `deadline` (and a per-account escalation nonce plus a `revoke_escalation` op for signer/co-signer/ancestors) to the Python envelope, or reword §5 so "the reference" is explicitly the EVM reference and note the Python gap.

### S-7 (Low) — Who may set an account's own policy is unspecified; both runners substitute the owner for "by: root, of: root"

**Location.** §4.4; `ledger.py:142-146` (`_may_administer`: owner or an ancestor's signer); `AgentAccounts.sol:392-400`; `tests/test_spec_vectors.py:130-136`; `SpecVectors.t.sol:210-213`; vector `tree-008` op 0.

**Description.** §4.4 covers ancestors and the owner but never says whether an account's own signer may replace its own policy (both references: no). `tree-008` says `setPolicy {by: "root", of: "root"}`; read literally that is the root's signer and both references would return Unauthorized, so both runners silently sign as the owner instead.

**Evidence.** `AuditSpecTest.test_S7_own_signer_cannot_set_own_policy`.

**Fix.** §4.4: "An account's signer MUST NOT change its own policy; only the owner or an ancestor's signer may." Vector format: allow `by: "owner"` and use it in `tree-008`.

### S-8 (Medium) — Python accepts `windowSecs = 0` (and negative), which disables `perWindowMax` for that account

**Location.** §2 (`uint32, ≥ 1`); `accounts.py:27-61` (no validation); `ledger.py:246-251, 255-260` (`set_policy`/`delegate` do not validate); EVM `_validatePolicy` rejects 0.

**Description.** With `window_secs = 0`, `spent()`'s cutoff equals `now`, so a spend recorded at `now` is never counted and the window cap is never enforced. An ancestor's signer can set this on a descendant (the ancestor's own window still binds, so the tree bound survives), and an owner can set it on a root.

**Evidence.** `test_S8_python_window_secs_zero_disables_per_window_max` — five spends of 100 in one second under `perWindowMax = 100`.

**Fix.** Validate `1 ≤ window_secs ≤ 2^32 − 1` in `Policy` (`__post_init__` or `from_dict`) and reject in `register_account`/`set_policy`/`delegate`; add a "policy invalid" vector.

### S-9 (Low) — Amount grammar in the x402 binding is not defined; the reference accepts non-canonical strings

**Location.** §7.2 table row `amount`; `policy_x402.py:75, 88, 93` (`int(...)`).

**Description.** `int()` accepts `"1_000"`, `" 1000 "`, `"+1000"`, Arabic-Indic digits and `True`. A strict implementation would report `malformed_payment`; the reference reports `ok` with amount 1000. The x402 SDK facilitators also use `int()` (`exact/facilitator.py:214`, `permit2_utils.py:237`), so the reference is consistent with what would settle, but the spec should say which it wants. Its own schema already defines a `uint` grammar.

**Evidence.** `test_S9_non_canonical_decimal_strings_are_accepted`.

**Fix.** §7.2: "Amount fields MUST match `^(0|[1-9][0-9]*)$`; otherwise `malformed_payment`." Implement with a regex before `int()`. Add a vector (`"1_000"` → `malformed_payment`).

### S-10 (Medium) — Permit2 `spender` is not checked, so `payee` can be meaningless; payer is never bound to the account

**Location.** §7.2 table; `policy_x402.py:86-90`; SDK `permit2_utils.py:203-215` (facilitator requires `spender == X402_EXACT_PERMIT2_PROXY_ADDRESS`), `constants.py` (`X402_EXACT_PERMIT2_PROXY_ADDRESS = 0x402085c2…0001`).

**Description.** In a Permit2 payment `witness.to` is only honoured by the x402ExactPermit2Proxy. The permit itself grants `spender` the right to move `permitted.amount` of the token. The binding reads `payee = witness.to` and ignores `spender`, so a wallet-side evaluator under an allow list approves a permit whose spender is an arbitrary contract that will send the tokens elsewhere. The SDK facilitator does check spender, so this is a wallet-side (and "policy service") hole, exactly where §7.2 says budget middleware belongs. Related: neither the spec nor the binding ties `authorization.from` / `permit2Authorization.from` to the account being evaluated. On the facilitator side the `extensions["spending-policy"].account` is payer-supplied, so a payer can charge an arbitrary account's window (denial of budget) or omit the extension to escape it; the spec must require the registry to map the payer address to the account and the facilitator to verify `from` against it.

**Evidence.** `test_S10_permit2_spender_unchecked` — spender `0xbadbad…` passes with `payee = 0xaaaa…`.

**Fix.** §7.2 table: add a row "`spender` (Permit2 only): MUST equal the x402ExactPermit2Proxy for the payment's network; otherwise `spender_mismatch`". Add "payer: `authorization.from` / `permit2Authorization.from`, canonicalised; an evaluator that knows which account it is evaluating MUST reject a payer that is not that account's payment address (`payer_mismatch`)". Implement both in `spend_of_exact`/`check_exact` (network → proxy address table; optional `expected_payer`). Add vectors for each.

### S-11 (Low) — `requirements_mismatch` compares five fields; the spec says "disagreeing"

**Location.** §7.2 second paragraph; `policy_x402.py:105-107` (`scheme, network, asset, amount/maxAmountRequired, payTo`).

**Evidence.** `test_S11_requirements_mismatch_ignores_extra_and_timeout` — different `maxTimeoutSeconds` and `extra` still `ok`.

**Fix.** State the compared fields in §7.2 (the five above is a reasonable choice; `extra` carries the EIP-712 domain and should probably be included since it changes what the signature covers).

### S-12 (Medium) — The proposed `extensions["spending-policy"]` shape does not follow the x402 v2 extension envelope

**Location.** §7.2 "Facilitator side"; SDK `extensions/eip2612_gas_sponsoring/server.py:11-27`, `extensions/payment_identifier/client.py:53-90`, `schemas/payments.py` (`PaymentRequired.extensions`, `PaymentPayload.extensions`).

**Description.** In the SDK every extension is declared by the resource server in `PaymentRequired.extensions` as `{"<key>": {"info": {...}, "schema": <JSON schema>}}` and the client echoes it in `PaymentPayload.extensions` with `info` filled in; a client only appends an extension the server declared. The spec proposes a flat `{"account": …, "registry": …}` volunteered by the payer with no server declaration and no schema. Key naming in the SDK is also mixed (`eip2612GasSponsoring`, `erc20ApprovalGasSponsoring` vs `payment-identifier`), so the spec should pick and state one.

**Fix.** Define it as `"spending-policy": {"info": {"account": "<id>", "registry": "<CAIP-2>:<contract-or-url>"}, "schema": {…}}`, declared by the server (facilitator-advertised via `/supported` extensions) and filled by the client; give the JSON schema; say what a facilitator does when the extension is present but the registry is unknown (ignore, per current text) versus declared-but-missing (reject?).

### S-13 (Medium) — §9 overstates what the x402 binding bounds; SVM `exact` is not "exact"

**Location.** §9 bullet 3 ("a compromised worker's key can lose at most what its ancestors allow"), §7.2 "Where the check runs", §1 "Spend"; SDK `mechanisms/svm/exact/facilitator.py:341-343` and `svm/exact/v1/facilitator.py:317-320` (`amount >= required` accepted).

**Description.** The loss-bound claim holds on the ledger path (§7.1 and the EVM contract), where the policy sits between the key and the value. Under plain x402 `exact` a compromised signer signs `transferWithAuthorization` directly; wallet-side middleware is not in the loop, the facilitator-side check is opt-in by the payer (S-10), and any facilitator or anyone with the signature can settle. The spec should say so plainly rather than let §9 imply the same bound. Two smaller points under the same heading: (a) §7.2 covers only EIP-3009/Permit2 payloads; a Solana `exact` payload (`{"transaction": …}`) is `malformed_payment` in the reference, and even a correct binding could not use `accepted.amount` because the SVM facilitator accepts any transfer ≥ the requirement — the spec should scope §7.2 to EVM payload types explicitly; (b) since the window records at signing time and settlement may lag by up to `validBefore`/`deadline` (SDK default validity 3600 s), wall-clock outflow in any `windowSecs` interval can reach 2 × `perWindowMax`; §9 should state that the bound is on *authorised* value by authorisation time.

**Fix.** §9: "Under §7.2 the policy bounds what a *policy-respecting* signer will authorise; it does not bound a signer whose key is used outside the wallet. The ledger path (§7.1, EVM reference) is the only path where the bound holds against a compromised key." §7.2: "This binding covers EVM `exact` payloads (EIP-3009 and Permit2). Other payload types are out of scope for 0.1 and MUST be reported as `malformed_payment`." Add the outflow note.

### S-14 (Low) — Solidity runner narrows vector integers with an unchecked cast; "MUST reject unrepresentable" has no vector and no reason code

**Location.** `SpecVectors.t.sol:287-288` (`uint128(vm.parseUint(...))`); `AgentAccounts.sol:366-370` (`PolicyViolation("amount too large")`, mapped by the runner to "unmapped reason"); §2 last paragraph.

**Description.** No vector carries a value ≥ 2^128, so nothing is wrong today, but if one were added the runner would silently truncate the policy fields (2^128 → 0) and the amount would surface as an unmapped string. The spec says a binary encoding MUST reject but gives no code, and the contract's rejection is a `PolicyViolation` with a string that is not a §3 code.

**Evidence.** `AuditSpecTest.test_S14_runner_cast_truncates_silently`.

**Fix.** Runner: `require(x <= type(uint128).max)` before casting (or `vm.assertLe`). Spec: either assign a code (`amount_unrepresentable`, checked before §3 step 1) or classify it with the zero-amount pre-check as "not a policy result" and say vectors never carry such values. Add a `check` vector with `amount = 2^128` marked `binaryReject` so the EVM runner asserts a revert and the Python runner asserts `ok`/`per_tx_exceeded` as appropriate.

### S-15 (Low) — Runner handling of `fund` and `setPolicy` is partly static

**Location.** `SpecVectors.t.sol:170-175, 194-196, 248-257`; `tests/test_spec_vectors.py:113-122, 130-137`; vector format header for `tree`.

**Description.** Solidity folds every `fund` into the child's delegation, asserts the op's `expect` equals `"ok"` as a string comparison (nothing is executed at op time), and `_pendingFund` matches on `to` only — a `fund` whose `from` is not the parent would be folded into the parent's delegation without complaint. Python runs `fund` as a raw ledger transfer to an in-tree address, which is a legitimate test of §4.3's "treat as internal move" clause, but the header comment says the reference "has no separate fund op" while the vector format lets `from`/`to` be any pair. `setPolicy` with `by == of` is executed as the owner in both runners (S-7). All of this is documented in the runner headers, so it is not concealment, but the `fund` `expect` is never computed by either implementation for a failing case.

**Fix.** Vector format: "`fund.from` MUST be `fund.to`'s parent". Solidity runner: assert that in `_pendingFund`. Consider a `fund` with insufficient balance expecting a non-`ok` result so the op is exercised (Python) or explicitly skipped with a reason (EVM).

### S-16 (Low) — `escalated = true` with `escalation = null` is unspecified

**Location.** §3 inputs; `accounts.py:81` (flag honoured regardless); `AgentAccounts.sol:415` (`BadEscalation`, not a policy code).

**Fix.** §3: "`escalated` MUST be false when `escalation` is `null`; an implementation that receives a co-signature for such a policy MUST reject it before the policy check (no §3 code)." Add a vector with `escalation: null, escalated: true` if the authors want a code instead.

### S-17 (Low) — Precedence among §7.2 binding codes is unspecified

**Location.** §7.2; `policy_x402.py:53-101` (order: version → `accepted` present → `requirements_mismatch` → `unsupported_scheme` → `malformed_payment` (requirements) → `malformed_payment` (payload) → `asset_mismatch` → `payee_mismatch` → `amount_mismatch`).

**Description.** §3 states that ordering only affects the reported reason, and the same is true here, but vectors are single-fault, so a second implementation with a different order passes the vectors and reports different codes on real multi-fault payments.

**Fix.** List the order above in §7.2 and add one two-fault vector (wrong payee and wrong amount → `payee_mismatch`).

### S-18 (Low) — §11 claims about other systems

**Location.** §11.

**Description.** (a) "x402's … batch-settlement extension" — in the SDK it is a *scheme* (`SCHEME_BATCH_SETTLEMENT = "batch-settlement"`, `mechanisms/evm/batch_settlement/constants.py:13`) built on deposits, vouchers and claims; it is closer to §7.1's deposit-is-the-spend model than to "a single authorisation". (b) "an `upto` authorisation can be the spend the policy sees" contradicts §7.2, which returns `unsupported_scheme` for `upto` (`x402-011`); the `upto` claim about bounding a single authorisation is itself consistent with the SDK (`ERR_UPTO_SETTLEMENT_EXCEEDS_AMOUNT`). (c) The statements about Coinbase spend permissions, Tempo and Kite are not verifiable from the material provided; I have not checked them and the draft should cite sources or drop them.

**Fix.** Rename "extension" to "scheme" for batch settlement and describe it as deposit-plus-vouchers; either add an `upto` row to §7.2 (spend = `permitted.amount`, the upper bound) or delete the composition sentence; add citations for the third-party claims.

### S-19 (Low) — Tree depth and list-size bounds are undocumented

**Location.** `AgentAccounts.sol:86, 89, 248, 510` (`MAX_LIST = 32`, `MAX_DEPTH = 16`); §4 has no such text; Python has neither bound.

**Fix.** §4.1: "Implementations MAY bound tree depth and list length and MUST document the bounds (EVM reference: depth 16, lists 32)."

### S-20 (Low) — Python wallet side does not do what §7.2 says a wallet does

**Location.** §7.2 "Wallet / signer side"; `accounts.py:188-211` (`AgentSigner` checks its own policy against its own window only); `agent.py:33-52` (records before ledger acceptance, rolls back on exception).

**Description.** The spec says the wallet "holds its own window and its ancestors' policies, or asks a policy service that does". `AgentSigner` holds only its own. Recording before acceptance with rollback satisfies §7.3 in effect but not in letter ("only once the payment is accepted").

**Fix.** Either note in §10 that the Python wallet side is single-level, or give `AgentSigner` the chain (as `check_exact` already takes one).

### Things checked and found fine

- §3 order and conditions: identical in `accounts.py:73-88` and `AgentAccounts.sol:484-490` (the contract omits step 1, impossible for uint; and pre-rejects zero amounts as §3 permits).
- §4.1 order and conditions: identical in `accounts.py:102-111` and `AgentAccounts.sol:494-507`.
- §4.2 walk order (A, P1 … root), first failure wins, check-all-then-record-all, `escalated` only for A: `ledger.py:132-140`, `AgentAccounts.sol:429-447`.
- §4.3 internal moves: Python treats an in-tree payee as a move (`ledger.py:130`); the contract cannot name an account as payee and blocks itself and modules (`AgentAccounts.sol:372-374`). Channel/pool payouts go only to the named payee/coordinator (`channels.py:81,152`, `pools.py:87`), so an in-tree payee keeps value in the tree.
- Refund/recall never touch windows (`AgentAccounts.sol:225-232, 270-279`; `ledger.py:271-281`). Delegation after a full parent window is still bound by the parent's window. Tightening a parent binds a wider child (`tree-008` on both). Lowering `perWindowMax` below current `spentInWindow` blocks further spends until aging.
- EVM window: never under-counts, over-count ≤ `bucketLen − 1 = ⌈W/31⌉ − 1` (within the spec's `B = ⌈W/31⌉`), ring reuse is stale iff `31·L ≥ W`, `sl.amount` cannot overflow because the merged slot is live and therefore already inside `spent ≤ perWindowMax ≤ uint128.max`; `spent + amount` is computed in uint256 so `check-029` genuinely tests non-wrapping.
- All 91 vectors: I recomputed every `check` (30), `within` (22), `window` (5), `tree` (8) and `x402-exact` (26) expectation by hand against the spec text; all are correct as written (`window-004` only under the caveat in S-1). The Solidity runner's escalation substitution (real co-signature from a test key) and window-bound check are faithful to §5/§6.2; its 6 skips match §10 exactly.
- EIP-3009 mapping (`accepted.amount`/`maxAmountRequired` = `authorization.value`, `payTo` = `authorization.to`, lowercased) matches the SDK facilitators (`exact/facilitator.py:206-221`, `exact/v1/facilitator.py:180-193`). Permit2 mapping of amount/token/`witness.to` matches `permit2_utils.py:217-266` except for `spender` (S-10). Using the evaluator's clock rather than `validBefore`/`deadline` is correct: the SDK checks those with a 6 s buffer for the settlement transaction only. Gas-sponsoring extensions (`eip2612GasSponsoring`, `erc20ApprovalGasSponsoring`) carry approvals to Permit2, not transfers, and need no policy treatment.

## Coverage table

| Rule | Vector ids |
|---|---|
| §2 uint128 fits / must not wrap | check-028, check-029 |
| §2 binary encoding MUST reject unrepresentable | none (S-14) |
| §2 `windowSecs ≥ 1`, `expiry` domain | none (S-2, S-8) |
| §2.1 canonical comparison (payee) | check-030 (runner-canonicalised, S-5), x402-015 |
| §2.1 canonical comparison (list entries) | none |
| §2.2 canonical encoding / policy id | none (S-4) |
| §3 #1 `negative_amount` | none |
| §3 #2 `expired` (inclusive boundary) | check-007, 008, 009, 016, 025; x402-007 |
| §3 #3 `payee_denied` | check-010, 011, 015, 017, 024; tree-005 |
| §3 #4 `payee_not_allowed` (incl. empty list) | check-012, 013, 014, 018; tree-004, 007; x402-006, 017 |
| §3 #5 `per_tx_exceeded` | check-001, 002, 019, 027; tree-001; x402-004, 019 |
| §3 #6 `per_window_exceeded` | check-004, 005, 006, 022, 023, 029; tree-001, 002, 006, 008; x402-005, 016 |
| §3 order 2→3→4→5→6 | check-015, 016, 017, 018, 019 |
| §3 zero amount | check-003, 009, 026 (EVM skips) |
| §3 escalation lifts #5 only | check-020–025; x402-018 |
| §3 `escalated` with `escalation = null` | none (S-16) |
| §4.1 #1–#5 | within-003/021; 004/022; 007–011; 012–014; 015–019 |
| §4.1 windowSecs / escalation not compared | within-005, 006, 020 |
| §4.1 checked on `setPolicy` | tree-007 |
| §4.2 walk order, first failure | tree-001, 002, 006 |
| §4.2 record in every ancestor | tree-001, 006 |
| §4.2 atomic: nothing recorded on failure | tree-002 |
| §4.2 `escalated` only for A (through a tree) | none (x402-019 covers it through a chain only) |
| §4.2 parent tightened below child | tree-008 |
| §4.2 differing `windowSecs` parent/child | none |
| §4.3 fund / recall not spends | tree-003 |
| §4.3 spend naming an in-tree account or contract | none |
| §4.4 ancestor sets descendant policy | tree-007 |
| §4.4 rotate signer, revoke escalation, recall from depth > 1, descendant has no authority upward, owner authority | none (tree-008 relies on runner substitution, S-7) |
| §5 signature contents, nonce, deadline, revocation | none (EVM runner produces a real co-signature but no vector exercises a bad one) |
| §6.1 exact window, boundary at `t = now − W` | window-001, 002, 003 |
| §6.1 window change | window-004, 005 (exactOnly; S-1) |
| §6.2 approximation bounds | none (runner-level check only) |
| §7.1 channel/pool deposit as spend, refund not un-spent | none |
| §7.2 amount mapping / `amount_mismatch` | x402-001, 009, 010, 023 |
| §7.2 payee mapping / `payee_mismatch` | x402-008, 015, 026 |
| §7.2 asset / `asset_mismatch` | x402-025 |
| §7.2 `now` not `validBefore` | x402-007 |
| §7.2 `malformed_payment` | x402-012, 013, 014, 021 |
| §7.2 `unsupported_scheme` | x402-011 |
| §7.2 `requirements_mismatch` (v2, v1) | x402-003, 022 |
| §7.2 v1 shape | x402-020–023 |
| §7.2 Permit2 | x402-024–026 |
| §7.2 Permit2 `spender`, payer binding | none (S-10) |
| §7.2 chain evaluation, escalation not for ancestors | x402-016, 017, 018, 019 |
| §7.2 binding-code precedence (multi-fault) | none (S-17) |
| §7.2 `extensions["spending-policy"]` | none |
| §7.3 recording only on acceptance | none |

---

## Round 2 — verification of commit 581e30c

Suites as committed: `python3 -m pytest tests -q` → 188 passed, 1 skipped; `FOUNDRY_PROFILE=session forge test` → 10 suites, 112 passed, 0 failed (SpecVectors: check 27 run/4 skipped, within 22/0, window 3/3, tree 8/0, policy 7/1; AuditSpec 5/5; WindowFuzz passes with the new oracle). Round-1 reproductions re-run against the new code: 9 of 11 now fail (i.e. the defects are gone); the two that still pass are the bare `SpendWindow` history test (see S-1 residual c) and the `id != spec_id` test (by design, see S-4). The three `id` vectors were recomputed independently and match. New residuals are reproduced in `scratchpad/test_audit_round2.py` (4 tests, all pass = all confirmed).

**Verdict per finding.**

- **S-1 — verified-closed for the round-1 scenario; three residuals, one of them worth fixing before 0.1 ships.** The wallet/ledger disagreement I constructed no longer exists: both sides prune under the outgoing length at the change (`ledger.py:255-256`, `accounts.py:287-291`, `agent.py:74`) and `AgentSigner.sign_update`'s evaluations can no longer change the answer, because under the new rule pruning under the window in force at *any* `u` is exact, so extra evaluations are idempotent. I agree with not adopting the pure definition: `_record` overwrites a slot only when it is dead under the window in force at that record time (`AgentAccounts.sol:471-480`), which is exactly the new rule's "aged out at some `u`", so the contract never under-counts the new §6.1 and would have under-counted the pure one. `WindowFuzz._exact` prunes at the change with the outgoing length at block time, matching the rule. Residuals: (a) *the formula is ambiguous at the change instant.* §6.1 says "`windowSecs(u)` is the length in force at `u`" but at `u = u_c` both lengths are "in force"; the implementation note pins it to the outgoing length, the formula does not. A spend at `t = u_c − W_old` is dropped under the outgoing length and kept under the incoming one — `test_R2_S1_change_instant_boundary…` (0 vs 100). Fix: "at the instant of a change the outgoing length applies (`windowSecs` is left-continuous)". (b) *A late-applied change under-counts on the wallet side.* The rule requires pruning with cutoff `u_c − W_old`; `AgentSigner.set_policy(policy, now)` prunes with cutoff `now − W_old` where `now` is whatever the caller passes. In the runners that is the ledger's change time, but a real wallet that learns of the change at receipt time `u' > u_c` and passes `u'` drops spends in `(u_c − W_old, u' − W_old]` that the ledger keeps — `test_R2_S1_late_applied_window_change_under_counts_on_the_wallet` (wallet 0, ledger 100, ledger rejects a spend the wallet signed). Fix: §6.1 (or §7.3): "an evaluator that applies a `windowSecs` change after the fact MUST prune as of the change time, not its own clock"; in Python, name the parameter `changed_at` and have `Agent.set_child_policy` pass the ledger time explicitly (it does today only by coincidence of the harness). (c) `SpendWindow.spent(now, W)` with a different `W` and no preceding `rewindow` still gives history-dependent answers (round-1 `test_S1_spentInWindow_depends_on_evaluation_history` still passes). Internal API only; both reference call paths do call `rewindow`. Consider having `SpendWindow` hold its own `window_secs` so `spent()` cannot be handed a different one.
- **S-2 — verified-closed for `expiry`; still-open for the zero address.** §2, schema `minimum: 1`, `Policy.__post_init__` (`expiry < 1` → `policy_invalid`), `policy-003`. The zero-address escalation is declared invalid in §2 and in the schema's *description* only: the `address` def has no pattern, `Policy.from_wire` accepts `"0x000…0"` (`test_R2_S2_python_accepts_zero_address_escalation`), and there is no `policy` vector for it. Fix: reject in `__post_init__`, add `policy-009`, and either give the schema an EVM pattern with a `not: {const: "0x0…0"}` or say the check is semantic. Minor: the Solidity runner skips `policy-003` by id (`SpecVectors.t.sol:119`) instead of having `_tryPolicy` return `false` for a wire-form `expiry` of 0 — `_tryPolicy` *is* the EVM decoder for the vectors, and it already rejects out-of-width values that way, so `policy-003` could run and assert `policy_invalid` like `policy-006`.
- **S-3 — verified-closed.** §2 now says Python bounds to 30 days (`accounts.py:26`, `MAX_WINDOW_SECS`, enforced in `__post_init__`) and the EVM accepts any uint32 (`AgentAccounts.sol:509-512`).
- **S-4 — verified-closed with one residual.** §2.2 now specifies field forms, null presence, bytewise sort, keyRef encoding; `Policy.wire()`/`spec_id`/`from_wire` exist; `id-001..003` are correct (recomputed). Residual: §2.2 says "addresses in canonical form" but `from_wire` does not canonicalise an address-form `escalation`, so `spec_id` differs between `0xEEEE…` and `0xeeee…` (`test_R2_S4_…`). Fix: canonicalise `escalation` in `__post_init__`. Also unstated: the case of `keyRef.key` hex (assume lowercase; say so).
- **S-5 — verified-closed.** `canonical_address` applied to lists in `__post_init__` and to the payee in `check` (`accounts.py:31-33, 141`); runner's `check` no longer lowercases; `check-031` tests a mixed-case list entry. Cosmetic: the tree runner still `.lower()`s the spend payee (`tests/test_spec_vectors.py:108`); harmless since `check` canonicalises, but it could go.
- **S-6 — verified-closed.** `ledger.py:169-173` requires an integer `escalation_deadline` in the co-signed body and rejects `now > deadline`; `Agent.submit` adds one. §5 now distinguishes the two references and calls the missing revoke path a gap against a SHOULD, which is accurate. Round-1 `test_S6_…` now fails with `Unauthorized`.
- **S-7 — verified-closed.** §4.4 text; vector format accepts `by: "owner"`; `tree-008` uses it; both runners branch on it (`test_spec_vectors.py:126`, `SpecVectors.t.sol:233-234`).
- **S-8 — verified-closed.** `__post_init__` enforces `1 ≤ window_secs ≤ 30 days`; `policy-002`.
- **S-9 — verified-closed.** `_uint` regex in `policy_x402.py:29,43-47`; `from_wire` regex; `policy-004/005`, `x402-027`. Round-1 `test_S9_…` now fails (all five forms rejected).
- **S-10 — verified-closed.** `PERMIT2_PROXY` (matches the SDK constant, lowercased) and `spender_mismatch`; `expected_payer` → `payer_mismatch`; `x402-028..030`; §7.2 rows and the facilitator-side MUST to map payer → account. One note, not a defect: the SDK constant is one address on every network today, and the spec says so; if that ever changes the check needs a per-network table.
- **S-11 — verified-closed.** §7.2 lists the compared fields; `_canon_req` includes `extra` (deep-frozen); `x402-032/033`.
- **S-12 — verified-closed.** §7.2 describes the `{info, schema}` envelope, server-declared, client-filled; `spending-policy.extension.schema.json` matches; key `spending-policy` is consistent with the SDK's `payment-identifier` style.
- **S-13 — verified-closed.** §7.2 scoped to EVM payload types with `x402-034` (SVM payload → `malformed_payment`); §9 rewritten to confine the compromised-key bound to the ledger path and to note the 2× wall-clock outflow.
- **S-14 — verified-closed with one small residual.** `_tryPolicy` checks grammar and widths before narrowing and treats an unrepresentable value as the decoder's rejection (`policy-006`); §2/§3 say unrepresentable amounts are decoder rejections, not policy results. Residual: `pol.expiry = uint64(vm.parseJsonUint(...))` (`SpecVectors.t.sol:336`) is still an unchecked narrowing; add `> type(uint64).max → false` for symmetry.
- **S-15 — verified-closed as stated.** Vector format says `fund.from` MUST be the parent; `_pendingFund` asserts it (`SpecVectors.t.sol:276-277`); the fold-into-delegation is stated in the header and §10. The `fund` op's `expect` is still a static `"ok"` on the EVM side; acceptable given the stated reason.
- **S-16 — verified-closed.** §3 note; `check_exact` raises `ValueError` (not a policy code) when `escalated` is set without a co-signer, consistent with the note.
- **S-17 — verified-closed.** §7.2 precedence list matches `spend_of_exact`/`check_exact` order exactly; `x402-031` is a two-fault vector.
- **S-18 — verified-closed.** §11 now states only what the SDK shows (`upto` cap; `batch-settlement` as a deposit/voucher scheme; `upto` is `unsupported_scheme` here) and defers the rest to the whitepaper. The remaining sentence about session keys and spend permissions is generic enough not to need a source.
- **S-19 — verified-closed.** §2 documents depth 16 / lists 32 for the EVM and none for Python.
- **S-20 — verified-closed.** §7.2 notes `AgentSigner` is single-level; §7.3 permits provisional record with rollback, which is what `Agent.submit` does.

**AuditSpec.t.sol comments.** Accurate for S-2, S-3, S-7 and S-14. S-1's comment ("under the corrected §6.1 the exact figure here is 0 … counting it is the over-count §6.2 permits; the contract never counts less") is correct. One wording nit in the S-2 comment: "the Python reference rejects it at load" is true for `expiry`; the sibling zero-address rule is not yet enforced there (see S-2 above).

**Still open after round 2, in priority order.** S-1(b) late-applied window change (spec sentence + parameter naming); S-1(a) change-instant boundary (one clause in §6.1); S-2 zero-address escalation (validation + vector); S-4 escalation canonicalisation; S-14 expiry narrowing in the runner; S-2 `policy-003` skip. None of these reopens a High: the two Highs are resolved for every path the references actually exercise.
