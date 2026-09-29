# AUDIT-1: AgentAccounts.sol

Date: 2026-09-29
Target: `contracts/src/AgentAccounts.sol` (Solidity 0.8.30, OpenZeppelin 5.4.0, Foundry)
Reproductions: `contracts/test/Audit1.t.sol` (tests marked `// FINDING A1-n` assert the desired
behaviour and fail against the current code; `test_ok_*` document what was checked and found correct)

## 1. Scope and method

Scope is the single contract `AgentAccounts` against its specification:

- `foliant/accounts.py` (`Policy.check`, `Policy.within`, `SpendWindow`, `AgentAccount`)
- `foliant/ledger.py` lines 73-282 (`register_account`, `lineage`, `_authorise`, `_may_administer`,
  `apply`, `_op_transfer`, `_op_delegate`, `_op_recall`, `_op_set_policy`, `_op_rotate_signer`)
- `docs/whitepaper.md` section 6.3a (the three hierarchical-budget invariants)

The deliberate differences listed in the contract header (EVM addresses instead of Ed25519 keys,
ERC-20 internal balances, EIP-712 escalation with a per-account escalation nonce, bounded lists,
locked modules) were taken as intended. The out-of-scope OpenZeppelin dependencies (`SafeERC20`,
`ReentrancyGuard`, `EIP712`, `ECDSA`) were assumed correct.

Method: the reference was read first, then the contract line by line, each function against the
questions "who can call it, what changes, what happens on revert, can it be reentered or
front-run, can a bound be hit, can any of the three tree invariants be broken by a sequence of
calls". Hypotheses were then tested with 21 new Foundry tests (`forge test --match-contract
Audit1Test`). The existing tests were read but not relied on. Nothing under `src/` was modified.

Severity scale: Critical (theft or permanent loss without preconditions), High (loss or denial of
service reachable by a semi-trusted party, or a broken protocol invariant), Medium (loss under a
realistic but non-default condition), Low (footgun, missing hardening, deviation with limited
impact), Informational.

## 2. Findings

### A1-1 High: unbounded spend window lets any descendant signer make every spend in its tree unaffordable

Lines: 65-66 (`Entry[] window`, `windowHead`), 361-386 (`_authorise`), 389-396 (`_spentAndPrune`),
286-292 (`spentInWindow`).

Every spend pushes one `Entry` into the window of the account and of every ancestor (line 380),
and every spend first reads every live entry of the account and of every ancestor (lines 393-395).
The number of live entries in an ancestor's window is bounded only by `perWindowMax / 1` over
`windowSecs`; with any token that has decimals, that bound is meaningless. Pruning is lazy and
itself linear in the number of stale entries, and entries are never deleted from storage.

Consequences:

1. Cost is O(live entries × depth) per spend, so an honest high-frequency tree (the use case in
   whitepaper 6.5) pays quadratically over a window. In the reproduction, 1,000 one-unit spends by
   a child cost 527M gas in aggregate, and a single sibling spend afterwards costs 2.58M gas
   (about 2,500 gas per live entry, storage cooled). At a 30M block limit roughly 12,000 live
   entries in a root window make every spend anywhere in that tree exceed the block.
2. This is a griefing vector. Any signer of any descendant, however tight its own `perTxMax`, can
   spend dust repeatedly and bloat the root's window. The attacker's own transactions get more
   expensive too, but it needs to succeed only once per batch; a signer that is a contract can
   batch hundreds of dust spends per transaction with warm reads. The tree's other agents then
   cannot spend for the duration of the root's `windowSecs`, and the first spend after that must
   pay the full linear prune in one transaction, which may itself exceed the block limit if the
   attacker pushed the count far enough on a chain with a higher limit than the victim's later
   transaction can use.
3. There is no owner remedy: `setPolicy` does not clear or shorten the walk (a shorter
   `windowSecs` still has to prune all stale entries), and there is no chunked-prune function.
   Since the only exits for value are `transfer` and `commit`, a tree whose root window cannot be
   read within a block has its funds locked for as long as that holds.

Failure sequence (test `test_A1_1_descendant_dust_spends_make_every_tree_spend_unaffordable`):
root (`windowSecs` 86,400) delegates to child C and sibling S; C's signer sends 1,000 spends of 1
unit; S's signer's next spend costs 2.58M gas. Extending the loop to 2,000 exhausts forge's
2^30 test gas limit in the loop itself.

Recommended fix: replace the per-entry array with a bounded structure. The simplest faithful
one is a fixed number of time buckets per account (say 16 or 32 slots of `windowSecs / N`
each, `(bucketStart, amount)` per slot), which makes read and write O(N × depth) with N a
constant, slightly over-counts at bucket granularity (conservative, never under-counts), and
needs no pruning. Alternatively keep a running `windowTotal` plus a ring of the last K entries
and refuse spends when the ring is full (bounded, exact, but K limits throughput). If the array
is kept, add a public `prune(id, maxSteps)` so anyone can amortise the walk, and reject
`amount < minSpend` per policy to cap the entry rate. Whatever is chosen, add a test asserting
spend gas is independent of other accounts' spend count.

### A1-2 Medium: deposit and refund credit the requested amount, not the amount received

Lines: 148-154 (`deposit`), 191-196 (`refund`).

`balanceOf[id][token] += amount` after `safeTransferFrom(msg.sender, this, amount)`. A
fee-on-transfer, deflationary or negatively rebasing token delivers less than `amount`, so the
sum of internal balances for that token exceeds the contract's holdings. All accounts in all
trees share one pool per token, so the shortfall is borne by whichever account withdraws last,
not by the depositor. The header says value is "any ERC-20", and stablecoins with pausable fee
switches exist (USDT has one), so this is realistic rather than theoretical.

Failure sequence (tests `test_A1_2_*`): two unrelated roots each deposit 1,000 of a 1%-fee
token; credited 2,000, held 1,980; the first `transfer` of 1,000 succeeds, the second reverts with
`ERC20InsufficientBalance(970, 990)` (the vault holds 980 and the outgoing transfer also burns).

Recommended fix: measure `balanceOf(this)` before and after the pull in `deposit` and `refund`
and credit the difference. Rebasing tokens still cannot be made safe with internal balances;
document them as unsupported. Consider a per-deployment token allow list if the agent chain
will only ever settle in a few stablecoins.

### A1-3 Low: a transfer to the contract itself strands the tokens

Lines: 160-171 (`transfer`), 176-188 (`commit`).

`payee == address(this)` passes every policy check (unless denied), debits the account and
`safeTransfer`s the tokens to the contract, where they are credited to nobody. There is no sweep
or rescue path, so they are lost. `address(0)` as payee is refused by OpenZeppelin's ERC20 but not
by every token, and would burn. Both are one-line checks. The same applies to `commit` if a
module names `address(this)`, though modules are trusted.

Reproduction: `test_A1_3_transfer_to_contract_itself_strands_funds`.

Recommended fix: `if (payee == address(this) || payee == address(0)) revert PolicyViolation(...)`
in `transfer` and `commit`.

### A1-4 Low: an escalation signature never expires and cannot be revoked

Lines: 73-74 (`ESCALATION_TYPEHASH`), 303-309 (`escalationDigest`), 342-357 (`_checkEscalation`).

The digest covers `(account, token, payee, amount, escalationNonce)` and `escalationNonce`
advances only when an escalated spend succeeds. A co-signature is therefore valid for ever:
across any number of non-escalated spends, a signer rotation, a `setPolicy` that keeps the same
co-signer, and any elapsed time. The co-signer has no way to withdraw an approval short of the
owner changing the policy's `escalation` address. In the reference the escalation key signs the
envelope body, which carries the account's transaction nonce, so any subsequent account
transaction invalidates an unused co-signature; the contract's header says the account nonce
"is replaced by msg.sender and the tx nonce", but for escalation that replacement lost the
implicit expiry.

The signature is correctly bound to the account, token, payee, amount, chain id and contract
(`test_ok_escalation_domain`), so this is a liveness-of-approval issue, not a replay.

Reproduction: `test_A1_4_escalation_signature_outlives_everything_but_a_cosigner_change`
(a year-old co-signature is used by a rotated-in signer after ten intervening spends and a policy
reset).

Recommended fix: add `uint64 deadline` to the typed data and require `block.timestamp <= deadline`;
optionally a `revokeEscalation(id)` callable by the co-signer or owner that bumps
`escalationNonce`. Consider also binding the digest to the current signer address if the
co-signer's approval is meant to be for a specific operator.

### A1-5 Low: `refund` emits no event

Lines: 191-196.

Every other balance change emits (`Deposited`, `Spent`, `Delegated`, `Recalled`); `refund` does
not, so internal balances cannot be reconstructed from logs and a module's returned escrow is
invisible to indexers. Reproduction: `test_A1_5_refund_emits_no_event`.

Recommended fix: `event Refunded(bytes32 indexed id, address indexed token, uint256 amount,
address module)` and emit it. Also reject `amount == 0` for consistency with `deposit`.

### A1-6 Informational: pruning is destructive, so lengthening `windowSecs` under-counts

Lines: 389-396, 429-440 (`_storePolicy`).

Entries pruned under a short window are skipped for good via `windowHead`; if the owner or an
ancestor later lengthens `windowSecs`, spend inside the new window that was pruned under the old
one is not counted. This matches the reference (`SpendWindow.spent` rebuilds the list), only
administrators can trigger it, and it can only ever under-count on the administrator's own
account or subtree, so it is not a privilege escalation. Recorded because a bucketed window
(A1-1 fix) can keep entries for the maximum permitted window and remove the effect.
Reproduction: `test_A1_6_pruned_entries_are_lost_when_window_is_lengthened`.

### A1-7 Informational: `commit` takes the module's word for `caller`

Lines: 176-188.

`caller` is a parameter supplied by the module; the contract checks it equals the signer but has
no evidence the signer initiated anything. A buggy or malicious module can commit any account's
balance to itself, within that account's policy, with no signer action. Modules are a locked,
deployer-chosen set, so this is a documented trust boundary, but the module contracts inherit the
full security burden for signer authentication and should be audited to that standard. The
error returned by `lockModules` to a non-deployer is `ModulesAlreadyLocked` even before locking,
which is misleading; use `Unauthorized`.
Reproduction: `test_A1_7_module_can_commit_without_signer_involvement`.

### A1-8 Informational: minor policy-shape and storage notes

- Lines 424-427: `perWindowMax < perTxMax` is accepted (a policy that can never spend its per-tx
  cap). Matches the reference; a `BadPolicy` here would catch operator mistakes.
- Lines 436-439: a policy with `hasAllowList = false` still stores its `allowList` array, and
  duplicate list entries consume `MAX_LIST` slots. Harmless; validating `allowList.length == 0`
  when `hasAllowList` is false and rejecting duplicates would tighten it.
- Line 380: `window` storage is never freed; `windowHead` only skips it. With A1-1 fixed this
  goes away.
- Lines 339-341: the `_checkEscalation` doc comment contradicts itself ("consumed whether or not"
  then "consumed only on success"); the second sentence is the true one.
- Line 385: `Spent.escalated` is `true` whenever a valid co-signature was supplied, even when the
  amount did not need it. Accurate to the input, but consumers should not read it as "exceeded
  perTxMax".

## 3. Checked and found correct

- Tree invariant 1 (every ancestor's policy and window bind a spend): `_authorise` walks the full
  lineage twice, check then record, and a failure anywhere reverts everything, including the
  escalation nonce (`test_ok_failed_spend_is_atomic_across_ancestors`). `_check` reproduces
  `Policy.check` in the same order with the same comparisons (`>=` on expiry, `>` on perTxMax,
  `spent + amount >` on perWindowMax).
- Tree invariant 2 (funding and recall are not spends): `delegate` and `recall` touch only
  `balanceOf` and never the windows. `recall(amount = 0)` is "everything", a no-op on an empty
  child, refuses self and non-descendants, and works from any depth (`test_ok_recall_zero_and_self`).
- Tree invariant 3 (any ancestor administers): `_mayAdminister` accepts the owner or any
  ancestor's signer; siblings, descendants and unrelated roots are refused. A root's policy is
  bounded by nothing but its owner, a child's by its current parent, from whichever ancestor sets
  it (`test_ok_ancestor_cannot_widen_beyond_parent`). This matches the reference, including its
  acceptance that a tightened middle node's existing children may be wider than it (runtime
  enforcement still binds them).
- Escalation lifts only `perTxMax` and only for the signing account; ancestors' caps, window caps,
  expiry and lists are unaffected (`test_ok_escalation_only_lifts_per_tx_max`). The digest is
  EIP-712 domain-separated (chain id and contract), bound to account, token, payee and amount,
  usable by the signer only, single-use across `transfer` and `commit`, and dead once the policy
  names a different co-signer (`test_ok_escalation_domain`, `test_ok_escalation_single_use_*`).
  OpenZeppelin `tryRecover` rejects high-s signatures. A signature supplied when the policy has
  no co-signer reverts rather than being ignored.
- Window pruning boundary equals the reference's `t > cutoff`: an entry at `t` counts through
  `t + windowSecs - 1` and drops at `t + windowSecs` (`test_ok_prune_boundary_matches_reference`).
  `windowSecs > block.timestamp` clamps the cutoff to 0 (`test_ok_window_longer_than_chain_age`).
  Head-pointer pruning is sound because entries are appended with a non-decreasing
  `block.timestamp`. `spentInWindow` agrees with `_spentAndPrune`.
- Casts and bounds: `amount` is capped at `uint128.max` before any window write, so
  `uint128(amount)` is safe; `uint64(block.timestamp)` and `uint32 windowSecs` are ample;
  window sums are `uint256`; `escalationNonce` cannot overflow in practice. `MAX_LIST` bounds
  every list loop at 32 × 32 comparisons; `MAX_DEPTH` bounds every lineage walk at 16 levels
  (root plus 15, the 17th level reverts `DepthExceeded`, `test_ok_max_depth_is_sixteen_levels`).
- `_within` matches `Policy.within` on all five compared axes, including: parent with an allow
  list requires a child allow list (an empty one is a valid subset and pays nobody,
  `test_ok_empty_allow_list_under_allow_listed_parent`); parent deny entries must each appear in
  the child's list (duplicates in the parent's list need one copy in the child's); a parent
  expiry requires a child expiry no later; `windowSecs` and `escalation` deliberately uncompared.
- Reentrancy: all four token-moving functions are `nonReentrant` and update state before the
  external call; the other state-changing functions make no external calls.
- Front-running: `register` derives the id from `msg.sender`, so an id cannot be squatted;
  `delegate` is parent-signer-only; `deposit` to an unregistered id reverts, so nothing can be
  locked by depositing early. An administrator front-running a spend with `setPolicy` or
  `rotateSigner` is the intended control, not a bug.
- Id domains: root ids (`"account", owner, signer, salt`) and child ids (`"child", parent,
  signer, salt`) cannot collide; the same address may be owner and signer; contracts may register
  (`test_ok_id_domains`).
- `delegate` with `fund == 0` may name any token; `fund > 0` requires the parent's balance in that
  token (`test_ok_delegate_with_unheld_token`). Child inherits the parent's owner.
- Token handling: OpenZeppelin 5.4 `SafeERC20` handles tokens that return nothing or `false`, and
  reverts on a non-contract token address. `refund` requires the module to have approved the
  contract, so a module cannot credit balances without moving tokens.
- Events: `Registered` (with parent), `Deposited`, `Spent`, `Delegated`, `Recalled`, `PolicySet`,
  `SignerRotated` carry the right fields (see A1-5 for the missing one).
- The reference's early return for payees inside the tree (`_tree_addresses`) has no counterpart
  because tree accounts have no addresses; the only intra-tree moves are `delegate` and `recall`,
  as the header states.

## 4. Verdict

The policy and tree logic is a faithful port of the reference and I could not break any of the
three hierarchical-budget invariants by any sequence of calls: no value leaves a tree beyond any
ancestor's policy and window, intra-tree moves are never spends, and administration follows the
lineage exactly.

The contract should not be deployed as is, for one reason: A1-1. The per-entry spend window is
correct but unbounded, and on a chain that is a denial-of-service that any delegated signer can
inflict on its whole tree, and a quadratic cost even for honest high-frequency use. It needs a
bounded window design before the next round. A1-2 should be fixed at the same time (cheap,
standard) or the supported token set restricted. A1-3 to A1-5 are small hardening items. Nothing
found allows theft or privilege escalation.

## 5. Resolution (author, 29 Sep 2026)

| Finding | Action |
| --- | --- |
| A1-1 High | Fixed. Window is now a fixed ring of 32 time buckets (`Slot[32]`); read and write cost is constant per account. Bucketing can only over-count (a live bucket keeps its earliest timestamp), by at most 2/32 of `windowSecs`; documented in the header. Reproduction now shows 207k gas for a spend after 1,000 dust entries, independent of count. |
| A1-2 Medium | Fixed. `deposit` and `refund` credit the balance delta actually received. |
| A1-3 Low | Fixed. `transfer` and `commit` reject `address(0)` and `address(this)` as payee (`BadPayee`). |
| A1-4 Low | Fixed. Escalation typed data now includes a `deadline`; `revokeEscalation(id)` (signer, owner or ancestor signer) bumps the nonce. |
| A1-5 Low | Fixed. `Refunded` event; zero refunds rejected. |
| A1-6 Info | Superseded by the bucketed window: stale buckets persist until overwritten, so lengthening a window no longer loses prior spend. |
| A1-7 Info | Accepted as the design's trust boundary; noted on `commit`. Modules will be audited to that standard. |
| A1-8 Info | Allow list stored only when `hasAllowList`; contradictory comment removed. `perWindowMax < perTxMax` and duplicate entries remain accepted (as in the reference). |

Reproduction tests in `test/Audit1.t.sol` were updated to assert the fixed behaviour (marked `FIXED`). Slither after the fix: `weak-prng` on the bucket index (false positive, annotated), `unused-return` on `tryRecover` (error value is checked), `timestamp` (inherent). Verification of the fixes by the auditor follows in section 6.

## 6. Verification (auditor, 29 Sep 2026, commit 5ea849d)

Method: re-read the contract in full; attacked the new `Slot[32]` window by hand (slot mapping,
liveness rule, ring coverage, policy changes, uint128 sums), the escalation deadline/revoke path
and the delta accounting; appended 9 tests to `test/Audit1.t.sol` (`test_V_*` verify, `test_A1_9*`
and `test_A1_10` reproduce new findings). `forge test`: 46 of 49 pass; the 3 failures are the new
reproductions and are kept, marked `// FINDING A1-9` / `// FINDING A1-10`.

### 6.1 Status of round-1 findings

| Finding | Status | Evidence |
| --- | --- | --- |
| A1-1 High, unbounded window | **Partially closed** | Gas is now constant: a sibling spend costs 187k gas on a fresh tree and 207k after 500 dust spends by a child (the 20k difference is a non-zero slot write, not a walk); a child's own spend after 500 own spends costs 187k (`test_V_A1_1_spend_gas_independent_of_prior_spends`). The DoS is gone. But the bucketed window that replaced the array under-counts (A1-9, A1-10 below), so the fix trades a liveness bug for a safety bug. |
| A1-2 Medium, delta accounting | **Closed** | `deposit`, `commit`→`refund` round-trip on a 1%-fee token credits exactly what arrived at each hop (990 → 981 → 972) and `Refunded` carries the received amount (`test_V_A1_2_delta_accounting`). Rebasing tokens remain unsupported, as documented. A reentrant donation during `_pull` can only raise the delta with the donor's own tokens; `nonReentrant` blocks a nested `deposit`. |
| A1-3 Low, self/zero payee | **Closed** | `BadPayee` on both `transfer` and `commit` for `address(0)` and `address(this)` (`test_V_A1_3_bad_payees`). |
| A1-4 Low, escalation lifetime | **Closed** (one note, A1-11) | `deadline` is in the typed data (a signature over `d` fails when called with `d+1`), `block.timestamp > deadline` reverts so `deadline == 0` means "already expired" (safe default, should be documented), the boundary `block.timestamp == deadline` is accepted. `revokeEscalation` works for signer, owner and any ancestor's signer, is refused to strangers and to non-lineage signers, and a re-signed message under the new nonce works (`test_V_A1_4_deadline_and_revoke`). |
| A1-5 Low, refund event | **Closed** | `Refunded(id, token, received, module)` emitted; `refund(…, 0)` reverts `ZeroAmount`. |
| A1-6 Info, destructive pruning | **Closed, superseded** | Buckets persist, so lengthening a window resurrects earlier spend (over-count, conservative; `test_A1_6_*` now sees 2000). The replacement mechanism has its own, worse, policy-change problem: A1-9b. |
| A1-7 Info, module trust | **Closed (accepted)** | Documented on `commit`. Nothing more to verify here; the module audits must cover signer authentication. |
| A1-8 Info | **Closed** | `allowList` no longer stored when `hasAllowList` is false (`policyOf(...).allowList.length == 0`); contradictory comment removed. `perWindowMax < perTxMax` and duplicates still accepted, as stated. |

### 6.2 New findings

#### A1-9 High: the 32-slot ring does not cover `windowSecs`, so a new spend can be merged under a stale timestamp and forgotten after seconds

Lines: 447-460 (`_record`), 437-443 (`spentInWindowOf`), 26-30 (header claim).

`bucketLen = ceil(W / 32)` and the slot is `(now / bucketLen) % 32`. The slot for the current
bucket B was last used by bucket B-32, whose latest timestamp is `(B-31)*bucketLen - 1`, i.e. at
least `31*bucketLen + 1` seconds ago. That is less than `W` whenever `31*ceil(W/32) < W`, which
holds for W = 3600 (3503 < 3600), 86400 (83700 < 86400), 63, 64, and most W above about 1000.
The old slot is then still live (`sl.ts > cutoff`), so line 454 adds the new amount to it and
keeps the OLD `ts`. The new amount is dropped when the old `ts` leaves the window, which is
`W - (now - old ts)` seconds later: 96 seconds on an hour window.

The header says bucketing "can only over-count". It is the reverse: this path and A1-10 only
under-count; the only over-count is resurrection after a window is lengthened.

Failure sequence (`test_A1_9_ring_wrap_merges_new_spend_under_old_timestamp`, W = 3600, cap
1000): signer spends 1 at t1 = last second of a bucket; at t2 = t1 + 3504 (first second of the
same slot's next cycle) spends 999 (allowed, window holds 1); at t3 = t1 + 3600, 96 seconds
after t2, `spentInWindow` is 0 and a further 1000 is accepted. 1999 left in 96 seconds against a
cap of 1000 per hour. Every ancestor with such a W is affected identically, so a delegated signer
can do this against the root's cap: tree invariant 1 is broken. The signer needs only to pick
timestamps, which it fully controls.

Variant A1-9b (`test_A1_9b_lengthening_window_lets_signer_double_spend_cap`): the same merge
happens when an administrator lengthens `windowSecs` (a tightening). The slot index remaps, a
new spend can land on a slot that is live under the old mapping, and is merged under that slot's
old `ts`. Owner changes W from 3200 to 6400; signer spends 1, then 999 at +1600 s, then 1000 at
+6400 s from the first spend, 4800 s after the 999 that the doubled window should still hold.

Recommended fix (also fixes A1-10 and A1-9b): make liveness conservative and the ring wide enough.

1. Store the bucket's nominal END, not the first timestamp: `end = bucketStart + bucketLen - 1`.
   A slot is live iff `end > cutoff`. Everything in the bucket is at or before `end`, so nothing
   is ever dropped before it is `W` old (over-count of at most one bucket, which is the
   conservative direction).
2. Merge with `sl.end = max(sl.end, curEnd)`; that keeps every merged amount alive at least as
   long as the newest one, which makes policy changes (any remapping) safe without clearing.
3. `bucketLen = ceil(W / (SLOTS - 1))`, so `31 * bucketLen >= W` and the slot being reused is
   always stale: `(B-31)*bucketLen - 1 <= now - W` for all `now >= B*bucketLen`.
4. Add a fuzz/invariant test that, for random W, random spend times and random policy changes,
   the sum of spends in ANY trailing `W` seconds never exceeds `perWindowMax` (compare against a
   reference list kept in the test). The existing invariant suite did not catch A1-9 because it
   never times spends against bucket edges; the reference comparison would.

#### A1-10 Medium: spends later in a bucket expire with the bucket's first spend

Lines: 445-446 (comment), 451-454.

A live slot keeps its first `ts`; amounts recorded up to `bucketLen - 1` seconds later are
dropped when that first `ts` leaves the window, i.e. up to `bucketLen - 1` seconds early (112 s
on an hour window, 45 minutes on a day window). Same exploit shape as A1-9 with a smaller gain:
spend 1 at the start of a bucket, `cap - 1` at its end, `cap` again at `first + W`; `2*cap - 1`
leaves in `W - bucketLen + 1` seconds (`test_A1_10_same_bucket_spends_expire_with_the_first`).
Medium because the excess rate is bounded by about 1/32 of the window per cycle, but it is still
a cap violation and it contradicts the documented "only over-count" guarantee. Fixed by item 1 of
the A1-9 recommendation.

#### A1-11 Informational: the co-signer cannot revoke its own approval, and revocation is silent

Lines: 339-344.

`revokeEscalation` is open to the signer, owner and ancestors' signers, but not to
`policy.escalation`, the party whose approval is being withdrawn (the round-1 recommendation
named the co-signer). With deadlines this is minor. `revokeEscalation` also emits no event, so
an indexer cannot tell a revocation from an escalated spend when the nonce moves. Suggest
allowing `msg.sender == a.policy.escalation` and emitting `EscalationRevoked(id, by, nonce)`.
Also document that `deadline == 0` means "expired" rather than "none".

### 6.3 Checked in this round and found correct

- Window gas: constant per account (32 cold slot reads, about 67k gas per lineage level); no
  per-spend storage growth; `windowHead`/array gone.
- uint128 slot sums cannot overflow: a live slot's amount is included in `spent`, `_check`
  enforces `spent + amount <= perWindowMax <= uint128.max` before `_record`, and a restarted slot
  holds `amount <= MAX_AMOUNT`. Verified at `perWindowMax = perTxMax = uint128.max`: one spend of
  `uint128.max` succeeds and the next 1-unit spend is a `PolicyViolation`, not a panic
  (`test_V_window_shorten_and_uint128_edge`).
- Shortening `windowSecs` never lets more than the cap leave inside the new window: old amounts
  are re-evaluated under the new cutoff (same test).
- An honest spend is never refused for longer than the reference would refuse it, because the
  current code never over-counts except after a window is lengthened (where the extra count is at
  most the spend that the lengthened window is meant to cover anyway). The "2/32 over-count"
  concern in the author's note does not arise; the real defect is the opposite direction (A1-9,
  A1-10).
- `_cutoff` clamps to 0 for windows longer than the chain's age; slots with `ts == 0` are never
  live.
- Escalation domain, single use across `transfer`/`commit`, atomic revert of the nonce on a
  failed spend, and all tree/administration properties from round 1 still hold (round-1 tests,
  adapted to the new signatures, pass).
- `_pull` delta accounting is `nonReentrant`-protected and cannot be inflated beyond tokens
  actually held.

### 6.4 Verdict

A1-2 through A1-8 are closed. A1-1 is closed as a denial of service but the replacement window
is unsound: A1-9 lets any signer, including a delegated one, spend roughly twice any ancestor's
`perWindowMax` inside a short interval on the most common window lengths (an hour, a day), and
A1-10 lets it exceed the cap by about 1/32 of the window per cycle. Both break the hierarchical
budget guarantee that the contract exists to enforce, and both are simple timing plays with no
preconditions beyond being a signer.

AgentAccounts is therefore NOT fit to proceed to the channel/pool modules or to Fuji until the
window is reworked as recommended under A1-9 (bucket-end liveness, max-merge, `ceil(W/31)`
buckets) and a reference-comparison fuzz test over random timestamps and policy changes is added
and green. The rework is small (one function and one constant) and the reproductions in
`test/Audit1.t.sol` must pass afterwards. A1-11 can be folded into the same change.

## 7. Resolution of the verification findings (author, 29 Sep 2026)

| Finding | Action |
| --- | --- |
| A1-9 High, A1-9b | Fixed. Buckets now record their nominal **end**; a slot is live iff `end > now - windowSecs`; on merge the later end is kept; `bucketLen = ceil(W / 31)` so the ring's period exceeds W and a reused slot is always stale. Value counts for at least W after it is recorded. |
| A1-10 Medium | Fixed by the same change: liveness by bucket end, so later spends in a bucket never expire before W. |
| A1-11 Info | Fixed. The co-signer may call `revokeEscalation`; `EscalationRevoked` event; deadline 0 documented as always expired. |

New test `test/WindowFuzz.t.sol`: 5,000 random sequences of spends at random times (concentrated at bucket edges and window boundaries) with administrator window changes in both directions, compared against an in-test copy of the reference `SpendWindow` (which prunes permanently at each check). Asserts the contract never counts less than the reference, and never more than everything recorded within `W + bucketLen` plus one further `Wmax + bucketLen` per window change. That last clause is the one behaviour worth knowing: after an administrator changes the window, value may count for longer than the new window (never shorter), because a live bucket can be merged under a later end. It is stated in the contract header. Reproductions A1-9/A1-9b/A1-10 now assert `>=` (never under-count) and pass; boundary tests were relaxed by one bucket length. 50 tests pass; Slither unchanged (three informational).

## 8. Verification, round 3 (auditor, 29 Sep 2026, commit 3173e83)

Method: re-read `Slot`, `_record`, `spentInWindowOf`, `_cutoff`, `revokeEscalation` and the header
claim; proved the window properties by hand (below); appended 5 tests to `test/Audit1.t.sol`
(`test_V3_*`) covering the edge widths, bucket-edge timings, the tightest ring reuse, a two-change
adversarial alternation with hand-placed merges, resurrection under a lengthened window, and the
co-signer revoke. `forge test`: 55 of 55 pass (35 in Audit1, 16 unit, 3 invariant, 1 window fuzz).
`src/` untouched.

### 8.1 Status

| Finding | Status | Evidence |
| --- | --- | --- |
| A1-9 High, ring did not cover W | **Closed** | `bucketLen = ceil(W/31)` gives `31*bucketLen >= W` for every uint32 W (checked at 1, 2, 31, 32, 33 and 2^32-1; overshoot < 31). At reuse the old slot's end is `(B-31)*bucketLen - 1 <= now - W` for all `now >= B*bucketLen`, so it is always stale. Tightest case tested (last second of bucket B-32, first second of B: 31*117+1 = 3628 > 3600). A1-9 and A1-9b reproductions pass. |
| A1-10 Medium, first-timestamp liveness | **Closed** | Liveness is by bucket END. A spend at the first second of a bucket counts for exactly W + bucketLen - 1 seconds, at the last second for exactly W (reference-exact); nothing is dropped before W. A1-10 reproduction passes. |
| A1-11 Info, co-signer revoke / event | **Closed** | `revokeEscalation` accepts `policy.escalation`; `EscalationRevoked(id, by, newNonce)` emitted; strangers still refused. |

### 8.2 Proofs requested

**Never-under-count, any sequence including admin changes.** A slot's `end` is set on restart to
`curEnd >= now` and otherwise only ever raised (`max`), so for every unit recorded at `t`,
`end >= t` for as long as the unit is in the slot. A slot is counted while `end > now - W_cur`,
hence at least while `now < t + W_cur`, which is exactly the reference's rule. A slot is
overwritten only when `end <= now - W_cur`, at which point every unit in it is at least `W_cur`
old and the reference has dropped it too. So no timing, with or without changes, drops a unit
before `W_cur` seconds. This does not depend on the ring period at all; the ring period only
bounds the over-count.

**Ring reuse is stale (same window).** Slot for bucket B was last written by bucket B-32 with
`end <= (B-31)*L - 1`. For any `now >= B*L`, `now - W >= B*L - W >= B*L - 31L = (B-31)*L > end`.
The `uint64` cast is safe: `end < now + L <= 2^40 + 2^28`.

**Maximum time a unit is counted.** Let the unit be recorded at `t` under `(W0, L0)`, with `c`
subsequent admin window changes each followed by a merge into the unit's live slot under
`(W_i, L_i)`, and `W_final` the window in force at the check. A merge can raise `end` only while
`end > now - W_i`, to at most `now + L_i - 1`, so by less than `W_i + L_i - 1`; a same-grid spend
never raises it (ring argument), so at most one raise per change. Therefore

    counted until  <  t + (L0 - 1) + sum_{i=1..c} (W_i + L_i - 1) + W_final
    counted until  >= t + W_final                                (never less)

If the unit was recorded straight into a live slot left over from an earlier, longer window,
`L0` is that earlier window's bucket length (the slot keeps the later end). Both terms are within
the fuzz test's `(w + mb) + changes*(wMax + mb)` with `mb = ceil(wMax/31)`, so the documented
bound is correct and slightly loose (by `c + 1` seconds and by using `wMax` for every term). An
admin alternating windows adversarially therefore adds at most one `(W + bucketLen)` of
over-count per change, and only ever against the signer's own budget. Verified with two
hand-placed merges (3600 -> 7200 -> 3600) in `test_V3_window_change_extension_bound` and a
30-day -> 1-hour resurrection in `test_V3_lengthen_resurrects_and_keeps_later_end`.

**Divide-before-multiply** (`bucket = now / L; curEnd = bucket*L + L - 1`): intentional floor to
the bucket start; the truncation is the point. No issue.

### 8.3 New findings

**A1-12 Informational: header understates the per-change over-count.** Line 35-36 says a change
may extend counting "up to one further window"; the exact bound is one further window plus one
bucket less one second, `W_i + L_i - 1`, and the base term after a change to a shorter window is
the earlier window's `bucketLen`, not the current one. The fuzz test already uses the correct
(`W + bucketLen`) form. Wording only.

No other findings. Nothing under Low.

### 8.4 Verdict

The spend window is now sound: it never counts less than the reference under any sequence of
spends and administrator changes, its over-count is bounded by the formula above and is always
in the conservative direction, and its cost is constant. All round-1 and round-2 findings are
closed (A1-7 accepted as a documented trust boundary). AgentAccounts is fit to proceed to the
channel and pool modules and to a Fuji deployment, on two conditions carried forward: the
modules must authenticate the signer themselves before calling `commit` (A1-7), and rebasing
tokens remain unsupported (A1-2 note). The channel/pool modules should be audited before mainnet
with the same adversarial pass, and this contract's `commit`/`refund` interplay with them
re-checked at that time.
