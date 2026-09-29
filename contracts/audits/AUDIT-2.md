# AUDIT-2: PaymentChannels.sol and Pools.sol

Date: 2026-09-29
Targets: `contracts/src/PaymentChannels.sol`, `contracts/src/Pools.sol` and their interaction with
`contracts/src/AgentAccounts.sol` (Solidity 0.8.30, OpenZeppelin 5.4.0, Foundry).
Reproductions: `contracts/test/Audit2.t.sol` (`forge test --match-contract Audit2Test`: 22 tests, 16 pass;
the 6 failures are the findings, marked `// FINDING A2-n`, and assert the desired behaviour; the
`test_verify_*` tests, marked `// VERIFY A2-...`, document what was attacked and found correct).
Nothing under `src/` was modified. The rest of the suite is unaffected (88 of 94 pass overall).

## 1. Scope and method

Specification taken as ground truth:

- `foliant/channels.py` (`verify_update`, `Channel`, streams excepted)
- `foliant/pools.py` (`Pool`, `PoolClaim`)
- `foliant/ledger.py` lines 191-240 (`_op_open_channel`, `_op_close_channel`, `_op_finalize_close`,
  `_op_join_pool`, `_op_begin_exit`, `_op_finalize_exit`) and 283-355 (payee/coordinator side)
- `docs/whitepaper.md` section 6.4 (payment channels) and 6.10 (pooled channels)
- AUDIT-1's two carried-forward conditions: modules must authenticate the signer before calling
  `commit` (A1-7), and the `commit`/`refund` interplay must be re-checked.

The deliberate differences listed in each contract's header (EIP-712 updates verified against the
account's current signer; msg.sender as the signer; streams not ported; coordinator bond not ported)
were taken as intended, with one exception noted at A2-4: where a reference-faithful behaviour
contradicts the whitepaper's stated guarantee, it is reported and the author can decide.

This is an independent review by a different reviewer from AUDIT-1. The contracts were read cold and
adversarially, against the list: signature domain and replay (across contracts, chains, channels,
after rotation, after close, after exit-and-rejoin), EIP-712 encoding, malleability, contract
signers, empty signatures; money flow (balance-delta measurement, reentrancy through token hooks,
`forceApprove`/`refund`, reverting payees and coordinators, fee-on-transfer); liveness and griefing
(open settle, signer-only close, timeouts and overflow); pool batches (revert-vs-skip, duplicates,
non-members, gas); window and escrow accounting. Every hypothesis with a plausible failure was then
written as a Foundry test with mock tokens (blacklist, fee-on-transfer, ERC-777-style hook) and a
reentering payee/coordinator/signer contract. The existing tests were read but not relied on.

Severity scale as in AUDIT-1: Critical (theft or permanent loss without preconditions), High (loss
or denial of service reachable by a semi-trusted party, or a broken protocol invariant), Medium (loss
under a realistic but non-default condition), Low (footgun, missing hardening, deviation with limited
impact), Informational.

## 2. Findings

### A2-1 High: updates are ordered by a payer-chosen `seq`, so the payer can void every unsettled update

Lines: `PaymentChannels.sol` 104 (`if (seq <= c.seq) revert StaleUpdate()`), 116 (`if (seq > c.seq) _apply`),
162-170 (`_apply`); `Pools.sol` 121, 135, 152, 199-205.

An update is applied iff `seq > c.seq`, `balance >= c.paid` and `balance <= deposit`. The payer
controls `seq` and may sign any `(seq, balance)` pair, so it can sign `(2^64-1, c.paid)`, which pays
nothing, and apply it (itself via `beginClose`/`beginExit`, or through anyone via `settle`). Every
update the payee holds now has a lower `seq` and is `StaleUpdate` for ever, whatever its balance.
The payee's only defence is to settle every off-chain update on-chain immediately, which is the
opposite of what a channel is for.

Whitepaper 6.4: "the payer may close by submitting its latest update and waiting `timeout` for the
payee to submit a *higher* one". Whitepaper 6.10 and `pools.py`'s docstring: "a member that exits
with a stale (low) update is contested by the coordinator within the window". Both guarantees
depend on "higher" meaning higher balance; the code makes it mean higher `seq`, which the cheating
party chooses. The reference `_apply_balance` / `Pool._apply` have the same ordering, so this is a
faithful port of a specification defect; it is reported because the whitepaper is the ground truth
and because on-chain it is exploitable for money.

Failure sequence (`test_A2_1_payer_seq_jump_invalidates_payees_unsettled_update`): channel with
deposit 1000; payee holds signed `(seq 7, balance 450)`; payer signs `(seq 2^64-1, balance 0)` and
calls `beginClose` with it; `settle(7, 450)` reverts `StaleUpdate`; after the timeout the payer
refunds the full 1000. Payee loses 450.

Pool variant (`test_A2_1b_pool_member_seq_jump_defeats_contest`): member joined with 500, coordinator
holds `(5, 400)`; member calls `beginExit` with `(2^64-1, 0)`; `contestExit(5, 400)` reverts
`StaleUpdate`; member exits with the full 500. Coordinator loses 400. The exit window and the
contest mechanism give the coordinator nothing here.

This also composes with A2-2's fix: whatever is done about epochs, ordering must not be under the
payer's control.

Recommended fix: order by balance, which is the only thing the honest counterparty cares about and
which the payer cannot lower. In `_apply`: `if (balance <= c.paid) revert StaleUpdate();` (strictly
greater; an equal balance carries no value and need not bump anything), keep `balance <= deposit`,
and drop the `seq > c.seq` gate (keep `seq` in the typed data for off-chain bookkeeping if wanted,
but never as an on-chain ordering). `settle` for pools then skips `balance <= c.paid` instead of
`seq <= c.seq`. The reference should be changed the same way. Update the existing tests that assert
seq semantics (`test_stale_and_forged_updates_rejected`, `test_stale_update_in_batch_is_skipped_not_fatal`).

### A2-2 High: an exited member's old updates replay against its new claim in the same pool

Lines: `Pools.sol` 96-109 (`join`: `_claims[id][account] = Claim(received, 0, 0, 0, false, true)`),
52-53 (`UPDATE_TYPEHASH` has no epoch), 118-122 (`settle`), 193-196 (`_verify`).

`join` is explicitly allowed for a member that has exited and resets the claim to `seq 0, paid 0`.
The typed data is `(pool, account, seq, balance)`: nothing in it distinguishes the first membership
from the second. Every update the member signed during the first membership is therefore a valid
update for the second, and applies as long as `balance <= newDeposit`. The coordinator (who holds
them all) or anyone else can submit the highest one and be paid a second time for value that was
already settled, or collect value the member had exited *without* paying (a legitimate exit on a
lower balance that the coordinator failed to contest in time now gets a second chance).

The existing test `test_settle_skips_exited_member_and_rejoin_starts_fresh` only checks the case
`oldBalance > newDeposit`, which reverts for the wrong reason, and so misses this.

Failure sequence (`test_A2_2_rejoin_replays_old_pool_update`): member joins with 500, signs
`(5, 400)`, coordinator settles it (paid 400, honest); member exits, refunded 100; member rejoins with
500 and has signed nothing; anyone submits the old `(5, 400)`: it applies, coordinator receives
another 400 from the new deposit. The reference `Pool.join` and `verify_update` behave identically.

Recommended fix: give each membership an epoch and put it in the signed data. Add `uint64 epoch` to
`Claim`, increment it on every `join` (keep the value across exit), and change the typehash to
`PoolUpdate(bytes32 pool,bytes32 account,uint64 epoch,uint64 seq,uint256 balance)` (or
`...uint256 balance` only, if A2-1's fix drops `seq`); verify against `c.epoch`. Alternatively
forbid rejoining (a member can use a child account or the coordinator a new pool), but the epoch is
cheap and keeps the documented behaviour. Channels are not affected: a closed channel keeps
`exists`, so a salt can never be reopened (`test_verify_closed_channel_id_not_reusable`).

### A2-3 Medium: the coordinator's unbounded `timeoutSecs` can lock every member's deposit for ever

Lines: `Pools.sol` 86-92 (`create`, only `timeoutSecs == 0` is refused), 141
(`c.exitAt = uint64(block.timestamp) + p.timeoutSecs`).

`timeoutSecs` is chosen by the coordinator, and only zero is rejected. The addition on line 141 is
checked `uint64` arithmetic, so with `timeoutSecs` above `2^64 - block.timestamp` every `beginExit`
panics; a merely enormous value (say 10^15 seconds) has the same practical effect without the panic.
There is no other path for a member's unspent deposit to leave the pool: the coordinator can only
take what the member signs, and the member can never exit. Members must inspect `timeoutSecs`
before joining; agents that join pools by id will not. A member is not extortable (signing does not
unlock the remainder) but its deposit is gone.

Failure sequence (`test_A2_3_pool_timeout_overflow_locks_member_deposits`): coordinator creates a
pool with `timeoutSecs = 2^64-1`; member joins with 500; `beginExit(…, "")` reverts with panic 0x11.

The channel side has the same arithmetic (`PaymentChannels.sol` 119) but there the payer chooses the
timeout and only its own close is affected; the payee can still settle
(`test_verify_timeout_bounds`). Low on that side, folded in here.

Recommended fix: a `MAX_TIMEOUT` (for example 30 days) enforced in `create` and `open`; also cast
through `uint256` so the sum cannot panic. Document the bound in the header.

### A2-4 Medium: a signer rotation voids the payee's unsettled updates (reference-faithful)

Lines: `PaymentChannels.sol` 156-159 (`_verify` checks `accounts.signerOf(c.payer)` at verification
time); `Pools.sol` 193-196.

Updates are verified against the account's *current* signer. The owner, or any ancestor's signer,
may rotate the signer at any time (`rotateSigner`). After a rotation every update the payee holds is
`Unauthorized`; the payer side then closes with no update and recovers everything. This is a second
way, independent of A2-1, for the payer side to renege on off-chain payments by an administrative
action that the payee cannot see coming or contest. The header lists "updates ... from the account's
current signer (looked up at verification, as the reference does)" as deliberate, and it is what
`ledger.py` does; but whitepaper 6.4's guarantee that the payee can always answer a close with its
higher update does not survive it. The existing test `test_signer_rotation_invalidates_old_updates`
asserts this behaviour as desired.

Failure sequence (`test_A2_4_signer_rotation_lets_payer_renege_on_unsettled_updates`): channel of
1000; payee holds `(3, 700)`; owner rotates the signer; new signer calls `beginClose` with no update;
`settle(3, 700)` reverts `Unauthorized`; the payer recovers 1000.

Recommended fix: snapshot the signer into the channel / claim at `open` / `join` and verify updates
against that (`c.signer`), so a rotation protects future channels without cancelling existing
obligations; the current signer continues to authorise `beginClose`/`finalizeClose` (msg.sender)
so a compromised key can still be locked out of *closing*. If the design intent really is that a
rotation cancels in-flight updates (a compromised key should not be able to drain a channel), then
the payee must be told so explicitly in the interface documentation, because it changes what an
off-chain update is worth. Either way, state it in the header and the whitepaper.

### A2-5 Low: a channel's payee (or a pool's coordinator) may be a module contract, stranding tokens

Lines: `AgentAccounts.sol` 372-374 (`_requirePayee` rejects `address(0)` and `address(this)` only);
`PaymentChannels.sol` 87 (`commit(..., payee, ...)`), 168 (`safeTransfer(c.payee, delta)`).

AUDIT-1's A1-3 fix stops value being paid to `AgentAccounts` itself, but the module addresses are
not covered. A channel with `payee == address(ch)` or `address(pools)` passes the policy (unless
denied), and every settlement self-transfers or transfers tokens into a module's balance that no
channel or claim accounts for. They can never be recovered. Only the payer's own signer can set it
up, so this is self-harm or an operator mistake, not theft. A pool coordinator is `msg.sender` of
`create`, which cannot be a module, so pools are only reachable as a channel payee.

Failure sequence (`test_A2_5_payee_may_be_a_module_and_strands_tokens`): `open(root, address(ch),
…, 300)`; `settle(1, 300)` succeeds; the channel contract holds 300 belonging to no channel.

Recommended fix: in `open`, `if (payee == address(this) || payee == address(accounts)) revert`, and
have `AgentAccounts.commit` also reject `isModule[payee]`. Cheap, and closes the last stranding path.

### A2-6 Informational: no ERC-1271, so a contract signer can open and close but never pay

Lines: `PaymentChannels.sol` 157, `Pools.sol` 194 (`ECDSA.tryRecover` only).

A smart-account or multisig signer can call `open`/`join`/`beginClose`/`finalizeClose` (msg.sender)
but cannot produce an update, so its channels can only ever refund
(`test_verify_contract_signer_cannot_sign_updates`). Consistent with the design (enclave-held keys),
and safe; recorded so the limitation is documented. If contract signers are wanted later, use
`SignatureChecker.isValidSignatureNow`.

### A2-7 Informational: the exit/close windows bound only `contestExit`; `settle` ignores them

Lines: `Pools.sol` 114-126 (`settle` skips only `!exists || exited`), 147-155 (`contestExit`);
`PaymentChannels.sol` 100-106.

After a member's exit window has elapsed, `contestExit` is refused but `settle` still applies a
higher update until `finalizeExit` runs; likewise a channel's `settle` works after `closingAt` until
`finalizeClose`. This matches the reference (`payee_settle_channel` has no window check; `Pool.settle`
neither), and it is not a loss for the payer side because it only ever pays what the payer signed.
But it means `contestExit` is redundant and the "window" gives the exiting party no deadline
certainty: it must finalize promptly or keep paying. Verified in
`test_verify_settle_after_exit_window` and `test_verify_settle_after_window_until_finalize`. If a
hard deadline is intended, add `if (c.exitAt != 0 && block.timestamp >= c.exitAt) continue;` in
`settle` (and the channel equivalent); otherwise remove `contestExit` or document the redundancy.

### A2-8 Informational: fee-on-transfer tokens leave the signed deposit above the escrowed one

Lines: `PaymentChannels.sol` 86-93, `Pools.sol` 104-107.

`deposit` is recorded as the amount received (correct, per A1-2), while the policy window records
the requested amount (conservative, correct) and the payer naturally signs balances up to the
requested amount. Any update with `balance > received` reverts "balance exceeds deposit", so the
last 1% of a fee token's channel is unpayable and the coordinator must drop such updates from a
batch or the whole batch reverts. No loss, no underflow (`test_verify_fee_on_transfer_accounting`
walks a full open-settle-close and join-exit on a 1% fee token: refund credits exactly what
arrives, allowance is fully consumed). Document, or expose `received` in the `Opened`/`Joined`
events (they already carry it) for the client to sign against.

## 3. Checked and found correct

- **Signer authentication before `commit` (A1-7 condition).** `open` and `join` pass `msg.sender`
  as `caller`, and `AgentAccounts.commit` checks `caller == a.signer` and `isModule[msg.sender]`; a
  stranger, the owner, or a direct call to `commit` is `Unauthorized`
  (`test_verify_open_and_join_authenticate_signer`).
- **`commit`/`refund` interplay.** `refund` is module-only, pulls exactly `remaining` under a
  `forceApprove` of exactly `remaining`, so no allowance is left over; `closed`/`exited` are set
  before the pull; the refund is credited to the committing account and is not a spend (window
  unchanged, matching the reference, which never reverses escrow returns); zero remainders skip the
  call, so a fully-paid channel or claim closes without hitting `ZeroAmount`
  (`test_verify_refund_not_a_spend_and_no_leftover_approval`, `test_verify_fee_on_transfer_accounting`).
- **Signature domain.** Separate EIP-712 domains (`FoliantPaymentChannels`/`FoliantPools`, version,
  chain id, contract) and separate type strings; a channel update fails on another channel, on a pool,
  and under another chain id; a pool update fails on a channel and another pool
  (`test_verify_signature_domains`). The digests match an independent encoding of the declared type
  strings with `uint64` fields padded per EIP-712 (`test_verify_eip712_encoding`).
- **Signature shapes.** High-s copies, empty, 64-byte and garbage signatures are `Unauthorized` in
  `settle`/`contestExit`; `beginClose`/`beginExit` treat an empty signature as "no update" and a
  non-empty bad one as an error rather than silently ignoring it (`test_verify_signature_shapes`).
  `ecrecover` returning zero is mapped to an error by OpenZeppelin, so an account whose signer is
  rotated to `address(0)` accepts nothing.
- **Channel id reuse.** `exists` is never cleared, so a closed channel's `(payer, payee, salt)` can
  never be reopened and old updates cannot land on a fresh channel
  (`test_verify_closed_channel_id_not_reusable`).
- **Balance-delta measurement in `open`/`join`.** Both are `nonReentrant`, `commit` is
  `nonReentrant`, and the only party whose tokens can raise the delta during the pull is a donor;
  the extra is credited to the channel/claim and ends up refunded to the payer account, not stolen.
- **Reentrancy on payout and refund.** An ERC-777-style payee that reenters `settle` or
  `finalizeClose` from its receive hook is blocked by the guard; state (`seq`, `paid`, `closed`,
  `exited`) is written before every external transfer; a hook calling `AgentAccounts.refund` is
  refused as a non-module; the refund pull goes to `AgentAccounts`, which has no hook
  (`test_verify_reentrancy_on_payout_and_refund`). The two modules and `AgentAccounts` have
  independent guards but share no state a cross-contract reentry could exploit.
- **Reverting payee / coordinator.** A blacklisted payee cannot be paid and cannot block the payer:
  `beginClose("")` and `finalizeClose` return the entire deposit
  (`test_verify_reverting_payee_does_not_block_close`). A blacklisted coordinator blocks `settle`
  (its own loss) and any update-carrying `beginExit`/`contestExit`, but `beginExit("")` and
  `finalizeExit` return every member's deposit (`test_verify_reverting_coordinator_does_not_block_exit`).
  Note this is the mirror of A2-1: the payer side keeps everything, which is correct when the payee
  cannot receive but is also the outcome the payer wants.
- **Open `settle`.** A third party can only apply the payer's own valid signatures with a higher
  seq and non-decreasing balance, which can only increase what the payee has received; it cannot
  lower `paid` or apply a stale one. `settle` remains available after `closingAt` until
  `finalizeClose`, matching the reference, so the payee can front-run a finalize with a higher
  update (`test_verify_settle_after_window_until_finalize`).
- **Signer-only close/finalize; lost keys.** `beginClose`/`finalizeClose`/`beginExit`/`finalizeExit`
  are signer-only, matching `ledger.py`'s signer ops; a lost signer key is recovered by the owner
  (or an ancestor's signer) rotating and the new signer closing
  (`test_verify_owner_rotation_recovers_stuck_channel`).
- **Timeouts.** Zero is refused in both contracts; `uint64(block.timestamp)` is ample; the
  overflow case is A2-3.
- **Pool batches.** Duplicate accounts apply once (the second is stale); a non-member entry is
  skipped *before* signature verification, so it cannot revert the batch; anyone may call; the
  coordinator receives one transfer of the total (`test_verify_pool_batch_shapes`). A bad signature
  or out-of-range balance reverting the whole batch is the reference behaviour; since only the
  member can sign for itself and the caller chooses the batch, a member cannot grief a settlement
  it is not in, and the coordinator simply omits bad updates. Gas is linear in batch size with one
  `signerOf` staticcall and one `ecrecover` per entry (the Slither `calls-loop` note is benign); the
  caller controls batch size.
- **Exit/close bookkeeping.** `deposit - paid` cannot underflow (`paid <= deposit` enforced on
  every apply, including the fee-on-transfer case where `deposit` is the received amount); a
  rejoined claim is a fresh struct (subject to A2-2); `_active` refuses exited members everywhere;
  `AlreadyMember` covers a member mid-exit.
- **Ids.** `channelId` and `poolId` are domain-tagged; the pool id is bound to `msg.sender`, so a
  coordinator cannot be impersonated at creation; a channel id cannot be squatted because only the
  payer's signer can open for that payer.
- **Reference parity** on everything not listed under findings: `_apply` conditions and their
  order, `beginClose`/`beginExit` semantics (verify, apply only if newer, set the deadline once),
  `finalizeClose`/`finalizeExit` boundaries (`now < closingAt` refuses; equality finalises),
  `contestExit` window (`now >= exitAt` refuses), `settle` skipping stale and exited members,
  `join` rejecting active members and zero deposits, the coordinator as `commit` payee for pools,
  and the open/join commit going through the full policy tree.

## 4. Verdict

The two modules are careful, compact ports and the mechanics that are hardest to get right on-chain
(escrow accounting, reentrancy, signature domains, refund interplay with `AgentAccounts`, reverting
counterparties) are correct; I could not make tokens appear, disappear, or move to a party that had
not been signed for, other than through the two protocol-level issues below. The A1-7 condition is
met: both modules pass `msg.sender` and `AgentAccounts.commit` authenticates it.

The two High findings are protocol issues rather than implementation slips, and both are inherited
from the Python reference: **A2-1** (payer-chosen `seq` ordering lets the payer, or a pool member,
void every unsettled update and renege on off-chain payments) and **A2-2** (an exited pool member's
old updates replay against its new claim). Together they mean that, as deployed, a channel update is
not a reliable claim on value and a pool coordinator can be paid twice. **A2-4** (signer rotation
voids in-flight updates) is a third, reference-faithful renege path that the design must either fix
or state plainly.

**Before Fuji testnet:** fix A2-1 (order by balance) and A2-2 (membership epoch in the signed
data), because the testnet exists to exercise the off-chain update flow between real agents and
payees, and with these two open that flow does not have the property the whitepaper promises;
testers would be validating the wrong protocol. Both fixes are small and localised to `_apply`,
`join` and the typehash, and the reference should be changed in step so the two stay in parity.
Update the existing tests that encode seq semantics. A2-3's `MAX_TIMEOUT` is a one-line guard that
should go in at the same time.

**Before mainnet:** decide and document A2-4 (snapshot the signer at open/join, or state that
rotation cancels in-flight updates); A2-5 (reject module addresses as payee); resolve A2-7 (either a
hard deadline in `settle` or drop `contestExit`); document A2-6 and A2-8; and re-run this file's
reproductions, which should then pass, plus a fuzz over random `(seq, balance)` sequences from a
*malicious* payer asserting the payee can always settle its highest-balance update until the channel
is closed. AUDIT-1's rebasing-token exclusion carries forward unchanged.

Nothing found is exploitable by an outsider without a signature from the account's signer; every
loss path runs from one counterparty of a channel or pool to the other.

## Resolution (author, 29 Sep 2026)

| Finding | Action |
| --- | --- |
| A2-1 High | Fixed, in the contracts **and the Python reference** (`channels._apply_balance`, `pools._apply`/`settle`/`begin_exit`): an update applies iff its balance is higher than what is settled; `seq` is carried for bookkeeping only. Reference test `test_payer_cannot_block_payee_by_jumping_seq`. |
| A2-2 High | Fixed in both: pool claims carry an `epoch` (incremented on every join) that is part of the signed update (`PoolUpdate(...uint64 epoch,...)`; Python body `epoch`); the node's pool view exposes it and the Python and TypeScript clients sign it. Reference test `test_rejoin_starts_a_new_epoch_and_old_updates_do_not_replay`. |
| A2-3 Medium | Fixed: `MAX_TIMEOUT = 30 days` enforced in `open` and `create`. |
| A2-4 Medium | Fixed: the account's signer at open/join is snapshotted in the channel/claim and is the key whose updates count; rotation moves only close/exit authority. |
| A2-5 Low | Fixed: `_requirePayee` also rejects locked module addresses. |
| A2-6 Info | Documented (no ERC-1271 in this version). |
| A2-7 Info | Kept as reference behaviour for testnet; listed for the mainnet review. |
| A2-8 Info | Documented. |

Reproduction tests in `test/Audit2.t.sol` now pass unchanged (they asserted the desired behaviour); the pre-existing tests that encoded seq ordering, rotation voiding updates and epoch-less rejoin were updated to the fixed semantics. Suite: 94 Foundry tests, 37 Python tests, TypeScript client 7, ElizaOS plugin 4, LangChain 17 — all green.
