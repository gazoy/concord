// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Test, Vm} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {MockERC20} from "./MockERC20.sol";

/// A fee-on-transfer token: 1% of every transfer is burned. Used to show that the internal
/// balances can exceed the tokens the contract actually holds (see AUDIT-1, A1-2).
contract FeeToken is ERC20 {
    constructor() ERC20("Fee", "FEE") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
    function _update(address from, address to, uint256 value) internal override {
        if (from != address(0) && to != address(0)) {
            uint256 fee = value / 100;
            super._update(from, address(0), fee); // burn the fee
            value -= fee;
        }
        super._update(from, to, value);
    }
}

/// Adversarial tests written for audit round 1 (contracts/audits/AUDIT-1.md).
/// Tests marked `// FINDING A1-n` assert the behaviour the contract SHOULD have and therefore
/// FAIL against the current code; they are the reproductions for the report. The others document
/// behaviour that was checked and found correct (or found to match the reference).
contract Audit1Test is Test {
    AgentAccounts acc;
    MockERC20 usdc;

    address owner = makeAddr("owner");
    address rootSigner = makeAddr("rootSigner");
    address childSigner = makeAddr("childSigner");
    address sibSigner = makeAddr("sibSigner");
    address payee = makeAddr("payee");
    address other = makeAddr("other");
    address module = makeAddr("module");
    uint256 escKey = 0xE5CA1A7E;
    address esc;

    function setUp() public {
        acc = new AgentAccounts();
        usdc = new MockERC20();
        esc = vm.addr(escKey);
        vm.warp(1_000_000);
    }

    // ------------------------------------------------------------------ helpers

    function _policy(uint128 tx_, uint128 win, uint32 secs) internal pure returns (AgentAccounts.PolicyInput memory p) {
        p.perTxMax = tx_;
        p.perWindowMax = win;
        p.windowSecs = secs;
    }

    function _register(address o, address s, AgentAccounts.PolicyInput memory p, uint256 salt) internal returns (bytes32) {
        vm.prank(o);
        return acc.register(s, p, salt);
    }

    function _fund(bytes32 id, uint256 amount) internal {
        usdc.mint(address(this), amount);
        usdc.approve(address(acc), amount);
        acc.deposit(id, address(usdc), amount);
    }

    function _delegate(bytes32 parent, address parentSigner, address s, AgentAccounts.PolicyInput memory p, uint256 salt, uint256 fund)
        internal
        returns (bytes32)
    {
        vm.prank(parentSigner);
        return acc.delegate(parent, s, p, salt, address(usdc), fund);
    }

    function _spend(bytes32 id, address signer, uint256 amount) internal {
        vm.prank(signer);
        acc.transfer(id, address(usdc), payee, amount, "", 0);
    }

    function _escSig(bytes32 id, address to, uint256 amount) internal view returns (bytes memory) {
        bytes32 digest = acc.escalationDigest(id, address(usdc), to, amount, acc.escalationNonce(id), type(uint64).max);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(escKey, digest);
        return abi.encodePacked(r, s, v);
    }

    // =========================================================== FINDINGS

    /// FINDING A1-1 (High): the spend window is an unbounded per-entry array read in full on
    /// every spend by the account and by every descendant. A descendant signer can bloat every
    /// ancestor's live window with dust spends, making every other spend in the tree cost gas
    /// linear in the number of entries, up to and beyond the block gas limit. Pruning is lazy
    /// and also linear, so the cost persists for the whole ancestor window and the recovery
    /// transaction after it is just as expensive.
    ///
    /// The test measures a sibling's spend after 1,000 one-unit spends by a child, with storage
    /// cooled so reads are priced as they would be in a fresh transaction. The assertion is what a
    /// bounded design would satisfy; it fails against the current contract: measured ~2.58M gas
    /// for one sibling spend after 1,000 dust entries, ~2,500 gas per live entry, so ~12k entries
    /// push every spend in the tree past a 30M block gas limit. (With n = 2,000 the dust loop
    /// itself exhausts forge's 2^30 test gas limit, which is the quadratic growth showing.)
    function test_A1_1_descendant_dust_spends_make_every_tree_spend_unaffordable() public {
        // a generous root: 1e18-unit tx cap, effectively unlimited window cap, one-day window
        bytes32 root = _register(owner, rootSigner, _policy(1e18, type(uint128).max, 86_400), 0);
        _fund(root, 1e24);
        bytes32 child = _delegate(root, rootSigner, childSigner, _policy(1e18, type(uint128).max, 1), 0, 1e6);
        bytes32 sib = _delegate(root, rootSigner, sibSigner, _policy(1e18, type(uint128).max, 86_400), 1, 1e6);

        uint256 n = 1000;
        for (uint256 i = 0; i < n; i++) {
            vm.prank(childSigner);
            acc.transfer(child, address(usdc), payee, 1, "", 0);
        }

        // a sibling's ordinary spend now has to walk the root's 1,000 live entries
        vm.cool(address(acc));
        vm.prank(sibSigner);
        uint256 g0 = gasleft();
        acc.transfer(sib, address(usdc), payee, 1000, "", 0);
        uint256 used = g0 - gasleft();
        emit log_named_uint("gas for one sibling spend after 1000 dust entries", used);
        emit log_named_uint("approx gas per live entry", (used - 60_000) / n);

        // A bounded window (fixed-size buckets, or a running total) keeps a spend O(depth).
        // 400k is generous for a two-level tree.
        assertLt(used, 400_000, "spend cost must not grow with other accounts' spend count");
    }

    /// FINDING A1-2 (Medium): deposit and refund credit the caller-supplied `amount`, not the
    /// amount received, so a fee-on-transfer (or deflationary/rebasing) token leaves the sum of
    /// internal balances above the contract's holdings. Every account holding that token shares
    /// one pool, so the shortfall lands on whichever account withdraws last, in any tree.
    function test_A1_2_fee_on_transfer_token_inflates_internal_balances() public {
        FeeToken fee = new FeeToken();
        bytes32 a = _register(owner, rootSigner, _policy(type(uint128).max, type(uint128).max, 3600), 0);
        bytes32 b = _register(other, sibSigner, _policy(type(uint128).max, type(uint128).max, 3600), 0);
        fee.mint(address(this), 2000);
        fee.approve(address(acc), 2000);
        acc.deposit(a, address(fee), 1000);
        acc.deposit(b, address(fee), 1000);

        uint256 credited = acc.balanceOf(a, address(fee)) + acc.balanceOf(b, address(fee));
        uint256 held = fee.balanceOf(address(acc));
        emit log_named_uint("credited", credited);
        emit log_named_uint("held", held);
        // the invariant a token vault must keep
        assertLe(credited, held, "internal balances exceed tokens held");
    }

    /// FINDING A1-2 (continued): the account that withdraws second cannot get its full balance.
    function test_A1_2b_fee_on_transfer_last_withdrawer_loses() public {
        FeeToken fee = new FeeToken();
        bytes32 a = _register(owner, rootSigner, _policy(type(uint128).max, type(uint128).max, 3600), 0);
        bytes32 b = _register(other, sibSigner, _policy(type(uint128).max, type(uint128).max, 3600), 0);
        fee.mint(address(this), 2000);
        fee.approve(address(acc), 2000);
        acc.deposit(a, address(fee), 1000);
        acc.deposit(b, address(fee), 1000);
        vm.prank(rootSigner);
        acc.transfer(a, address(fee), payee, 990, "", 0); // a takes its full credited balance (1% fee: 990 arrived)
        // FIXED: b was credited what arrived (990), so it cannot draw 1000 and the vault stays whole
        assertEq(acc.balanceOf(b, address(fee)), 990);
        vm.prank(sibSigner);
        vm.expectRevert(AgentAccounts.InsufficientFunds.selector);
        acc.transfer(b, address(fee), payee, 1000, "", 0);
    }

    /// FINDING A1-3 (Low): a transfer whose payee is the contract itself passes every check and
    /// moves the tokens into the contract with no internal balance credited; they are stranded
    /// forever (there is no sweep). A denied `address(this)` (and address(0)) costs one line.
    function test_A1_3_transfer_to_contract_itself_strands_funds() public {
        bytes32 root = _register(owner, rootSigner, _policy(1000, 1000, 3600), 0);
        _fund(root, 1000);
        // FIXED: the contract refuses itself and the zero address as payees
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.BadPayee.selector);
        acc.transfer(root, address(usdc), address(acc), 1000, "", 0);
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.BadPayee.selector);
        acc.transfer(root, address(usdc), address(0), 1000, "", 0);
        assertEq(acc.balanceOf(root, address(usdc)), 1000);
    }

    /// FINDING A1-4 (Low): an escalation signature never expires and cannot be revoked. It is
    /// bound only to (account, token, payee, amount, escalationNonce); the nonce advances only on
    /// escalated spends, so any number of ordinary spends, a signer rotation and a policy reset
    /// that keeps the same co-signer all leave an old signature live. The reference binds the
    /// co-signature to the account's tx nonce, so any later account transaction invalidates it.
    function test_A1_4_escalation_signature_outlives_everything_but_a_cosigner_change() public {
        AgentAccounts.PolicyInput memory p = _policy(100, 100_000, 3600);
        p.escalation = esc;
        bytes32 root = _register(owner, rootSigner, p, 0);
        _fund(root, 100_000);
        bytes memory sig = _escSig(root, payee, 5000); // co-signer approves one 5000 spend "now"

        // time passes, many ordinary spends happen, the signer is rotated, the policy is re-set
        vm.warp(block.timestamp + 365 days);
        for (uint256 i = 0; i < 10; i++) _spend(root, rootSigner, 100);
        vm.prank(owner);
        acc.rotateSigner(root, other);
        p.perTxMax = 50;
        vm.prank(owner);
        acc.setPolicy(root, p);

        // ...and the year-old signature still authorises the spend for the new signer
        vm.prank(other);
        acc.transfer(root, address(usdc), payee, 5000, sig, type(uint64).max);
        assertEq(usdc.balanceOf(payee), 1000 + 5000);
    }

    /// FINDING A1-5 (Low): `refund` changes an account's balance without emitting any event, so
    /// off-chain accounting cannot reconstruct balances from logs.
    function test_A1_5_refund_emits_no_event() public {
        bytes32 root = _register(owner, rootSigner, _policy(1000, 1000, 3600), 0);
        _fund(root, 1000);
        address[] memory mods = new address[](1);
        mods[0] = module;
        acc.lockModules(mods);
        vm.prank(module);
        acc.commit(root, rootSigner, address(usdc), payee, 500, "", 0);
        vm.startPrank(module);
        usdc.approve(address(acc), 500);
        vm.recordLogs();
        acc.refund(root, address(usdc), 500);
        vm.stopPrank();
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256 fromAcc;
        for (uint256 i = 0; i < logs.length; i++) if (logs[i].emitter == address(acc)) fromAcc++;
        assertEq(acc.balanceOf(root, address(usdc)), 1000);
        assertEq(fromAcc, 1, "refund should emit an event");
    }

    /// FINDING A1-6 (Informational, matches the reference): pruning is destructive. Entries pruned
    /// under a short window are gone for good, so lengthening `windowSecs` afterwards under-counts.
    function test_A1_6_pruned_entries_are_lost_when_window_is_lengthened() public {
        bytes32 root = _register(owner, rootSigner, _policy(1000, 1000, 60), 0);
        _fund(root, 10_000);
        _spend(root, rootSigner, 1000);
        vm.warp(block.timestamp + 61);
        _spend(root, rootSigner, 1000); // prunes the first entry
        vm.prank(owner);
        acc.setPolicy(root, _policy(1000, 1000, 3600));
        // FIXED (bucketed window keeps stale buckets until overwritten): the hour-long window sees both
        assertEq(acc.spentInWindow(root), 2000);
    }

    /// FINDING A1-7 (Informational, trust assumption): `commit` takes the module's word for
    /// `caller`. A module never calls back to the signer, so a buggy or malicious module can commit
    /// an account's balance to itself within policy without the signer having done anything.
    function test_A1_7_module_can_commit_without_signer_involvement() public {
        bytes32 root = _register(owner, rootSigner, _policy(1000, 1000, 3600), 0);
        _fund(root, 1000);
        address[] memory mods = new address[](1);
        mods[0] = module;
        acc.lockModules(mods);
        vm.prank(module); // no prank as rootSigner anywhere
        acc.commit(root, rootSigner, address(usdc), payee, 1000, "", 0);
        assertEq(usdc.balanceOf(module), 1000);
    }

    // ====================================================== CHECKED, CORRECT

    /// Pruning boundary is exactly the reference's `t > cutoff`: an entry at t drops at t+window.
    function test_ok_prune_boundary_matches_reference() public {
        bytes32 root = _register(owner, rootSigner, _policy(1000, 1000, 3600), 0);
        _fund(root, 10_000);
        _spend(root, rootSigner, 1000);
        vm.warp(block.timestamp + 3599);
        assertEq(acc.spentInWindow(root), 1000);
        vm.prank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "amount would exceed per_window_max"));
        acc.transfer(root, address(usdc), payee, 1, "", 0);
        vm.warp(block.timestamp + 1); // now == t + window: cutoff == t, entry dropped
        assertEq(acc.spentInWindow(root), 0);
        _spend(root, rootSigner, 1000);
    }

    /// windowSecs larger than block.timestamp: cutoff clamps to 0 and everything counts.
    function test_ok_window_longer_than_chain_age() public {
        vm.warp(100);
        bytes32 root = _register(owner, rootSigner, _policy(1000, 1000, type(uint32).max), 0);
        _fund(root, 10_000);
        _spend(root, rootSigner, 600);
        assertEq(acc.spentInWindow(root), 600);
        vm.prank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "amount would exceed per_window_max"));
        acc.transfer(root, address(usdc), payee, 500, "", 0);
    }

    /// An escalation signature is bound to the account, chain and contract; it cannot be moved to a
    /// sibling with the same co-signer, replayed on another chain id, or used by a non-signer.
    function test_ok_escalation_domain() public {
        AgentAccounts.PolicyInput memory p = _policy(100, 100_000, 3600);
        p.escalation = esc;
        bytes32 a = _register(owner, rootSigner, p, 0);
        bytes32 b = _register(owner, sibSigner, p, 1);
        _fund(a, 10_000);
        _fund(b, 10_000);
        bytes memory sig = _escSig(a, payee, 500);
        vm.prank(sibSigner);
        vm.expectRevert(AgentAccounts.BadEscalation.selector);
        acc.transfer(b, address(usdc), payee, 500, sig, type(uint64).max);
        vm.prank(other);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.transfer(a, address(usdc), payee, 500, sig, type(uint64).max);
        uint256 cid = block.chainid;
        vm.chainId(cid + 1);
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.BadEscalation.selector);
        acc.transfer(a, address(usdc), payee, 500, sig, type(uint64).max);
        vm.chainId(cid);
        // a co-signer change kills it
        p.escalation = other;
        vm.prank(owner);
        acc.setPolicy(a, p);
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.BadEscalation.selector);
        acc.transfer(a, address(usdc), payee, 500, sig, type(uint64).max);
        // and a different token
        p.escalation = esc;
        vm.prank(owner);
        acc.setPolicy(a, p);
        MockERC20 dai = new MockERC20();
        dai.mint(address(this), 1000);
        dai.approve(address(acc), 1000);
        acc.deposit(a, address(dai), 1000);
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.BadEscalation.selector);
        acc.transfer(a, address(dai), payee, 500, sig, type(uint64).max);
    }

    /// The same escalation signature serves either transfer or commit, but only once between them.
    function test_ok_escalation_single_use_across_transfer_and_commit() public {
        AgentAccounts.PolicyInput memory p = _policy(100, 100_000, 3600);
        p.escalation = esc;
        bytes32 a = _register(owner, rootSigner, p, 0);
        _fund(a, 10_000);
        address[] memory mods = new address[](1);
        mods[0] = module;
        acc.lockModules(mods);
        bytes memory sig = _escSig(a, payee, 500);
        vm.prank(module);
        acc.commit(a, rootSigner, address(usdc), payee, 500, sig, type(uint64).max);
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.BadEscalation.selector);
        acc.transfer(a, address(usdc), payee, 500, sig, type(uint64).max);
    }

    /// MAX_DEPTH = 16 levels including the root (depth indices 0..15); the 17th level is refused.
    function test_ok_max_depth_is_sixteen_levels() public {
        bytes32 cur = _register(owner, rootSigner, _policy(1, 1, 1), 0);
        address s = rootSigner;
        uint256 levels = 1;
        while (true) {
            address ns = address(uint160(0x1000 + levels));
            vm.prank(s);
            try acc.delegate(cur, ns, _policy(1, 1, 1), 0, address(usdc), 0) returns (bytes32 id) {
                cur = id;
                s = ns;
                levels++;
            } catch (bytes memory err) {
                assertEq(bytes4(err), AgentAccounts.DepthExceeded.selector);
                break;
            }
        }
        assertEq(levels, acc.MAX_DEPTH());
    }

    /// Ancestor administration: an ancestor's signer cannot widen a descendant beyond its parent,
    /// even when that ancestor is above the parent, and an unrelated root signer cannot administer.
    function test_ok_ancestor_cannot_widen_beyond_parent() public {
        bytes32 root = _register(owner, rootSigner, _policy(500, 1000, 3600), 0);
        bytes32 child = _delegate(root, rootSigner, childSigner, _policy(100, 100, 3600), 0, 0);
        bytes32 grand = _delegate(child, childSigner, sibSigner, _policy(10, 10, 3600), 0, 0);
        vm.prank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.BadPolicy.selector, "child per_tx_max exceeds parent"));
        acc.setPolicy(grand, _policy(101, 10, 3600));
        vm.prank(rootSigner);
        acc.setPolicy(grand, _policy(100, 100, 3600));
        bytes32 root2 = _register(other, other, _policy(500, 1000, 3600), 0);
        assertTrue(root2 != root);
        vm.prank(other);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.setPolicy(grand, _policy(1, 1, 1));
    }

    /// Allow-list edge cases: parent with an allow list, child with hasAllowList and an empty list
    /// is accepted (subset) and can pay nobody; a child with hasAllowList=false is refused.
    function test_ok_empty_allow_list_under_allow_listed_parent() public {
        AgentAccounts.PolicyInput memory pp = _policy(500, 1000, 3600);
        pp.hasAllowList = true;
        pp.allowList = new address[](1);
        pp.allowList[0] = payee;
        bytes32 root = _register(owner, rootSigner, pp, 0);
        _fund(root, 1000);
        AgentAccounts.PolicyInput memory cp = _policy(100, 100, 3600);
        cp.hasAllowList = true; // empty list
        bytes32 child = _delegate(root, rootSigner, childSigner, cp, 0, 500);
        vm.prank(childSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "payee is not on the allow list"));
        acc.transfer(child, address(usdc), payee, 1, "", 0);
        // but its funds can still be recalled
        vm.prank(rootSigner);
        acc.recall(root, child, address(usdc), 0);
        assertEq(acc.balanceOf(root, address(usdc)), 1000);
    }

    /// Duplicate list entries and lists past MAX_LIST.
    function test_ok_list_bounds_and_duplicates() public {
        AgentAccounts.PolicyInput memory p = _policy(500, 1000, 3600);
        p.denyList = new address[](33);
        vm.prank(owner);
        vm.expectRevert(AgentAccounts.ListTooLong.selector);
        acc.register(rootSigner, p, 0);
        p.denyList = new address[](32);
        for (uint256 i = 0; i < 32; i++) p.denyList[i] = other; // duplicates fill the whole list
        bytes32 root = _register(owner, rootSigner, p, 0);
        _fund(root, 100);
        vm.prank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "payee is denied"));
        acc.transfer(root, address(usdc), other, 1, "", 0);
        // a child must carry the (deduplicated) parent deny entries: one copy suffices
        AgentAccounts.PolicyInput memory cp = _policy(1, 1, 1);
        cp.denyList = new address[](1);
        cp.denyList[0] = other;
        _delegate(root, rootSigner, childSigner, cp, 0, 0);
    }

    /// Delegate may name a token the parent never held when fund == 0; with fund > 0 it needs balance.
    function test_ok_delegate_with_unheld_token() public {
        bytes32 root = _register(owner, rootSigner, _policy(500, 1000, 3600), 0);
        vm.prank(rootSigner);
        acc.delegate(root, childSigner, _policy(1, 1, 1), 0, address(0xDEAD), 0);
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.InsufficientFunds.selector);
        acc.delegate(root, childSigner, _policy(1, 1, 1), 1, address(0xDEAD), 1);
    }

    /// recall(amount = 0) on an empty child is a no-op, and recall of self is refused.
    function test_ok_recall_zero_and_self() public {
        bytes32 root = _register(owner, rootSigner, _policy(500, 1000, 3600), 0);
        bytes32 child = _delegate(root, rootSigner, childSigner, _policy(1, 1, 1), 0, 0);
        vm.prank(rootSigner);
        acc.recall(root, child, address(usdc), 0);
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.NotDescendant.selector);
        acc.recall(root, root, address(usdc), 0);
        // grandchild recall skipping a level
        bytes32 grand = _delegate(child, childSigner, sibSigner, _policy(1, 1, 1), 0, 0);
        _fund(grand, 7);
        vm.prank(rootSigner);
        acc.recall(root, grand, address(usdc), 0);
        assertEq(acc.balanceOf(root, address(usdc)), 7);
    }

    /// Escalation lifts perTxMax but never perWindowMax, expiry, or lists.
    function test_ok_escalation_only_lifts_per_tx_max() public {
        AgentAccounts.PolicyInput memory p = _policy(100, 1000, 3600);
        p.escalation = esc;
        p.denyList = new address[](1);
        p.denyList[0] = other;
        bytes32 root = _register(owner, rootSigner, p, 0);
        _fund(root, 10_000);
        bytes memory sig = _escSig(root, payee, 1001);
        vm.prank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "amount would exceed per_window_max"));
        acc.transfer(root, address(usdc), payee, 1001, sig, type(uint64).max);
        sig = _escSig(root, other, 500);
        vm.prank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "payee is denied"));
        acc.transfer(root, address(usdc), other, 500, sig, type(uint64).max);
        assertEq(acc.escalationNonce(root), 0);
    }

    /// Root ids and child ids live in separate domains; a registered root cannot be re-registered by
    /// another owner and the same owner/signer pair yields distinct ids per salt.
    function test_ok_id_domains() public {
        bytes32 r0 = _register(owner, rootSigner, _policy(1, 1, 1), 0);
        bytes32 r1 = _register(owner, rootSigner, _policy(1, 1, 1), 1);
        assertTrue(r0 != r1);
        vm.prank(owner);
        vm.expectRevert(AgentAccounts.AccountExists.selector);
        acc.register(rootSigner, _policy(1, 1, 1), 0);
        bytes32 c = _delegate(r0, rootSigner, rootSigner, _policy(1, 1, 1), 0, 0);
        assertTrue(c != r0 && c != r1);
        assertEq(acc.childId(r0, rootSigner, 0), c);
        // same owner and signer address is allowed
        bytes32 s = _register(other, other, _policy(1, 1, 1), 0);
        assertEq(acc.ownerOf(s), other);
        assertEq(acc.signerOf(s), other);
    }

    /// A refused spend leaves neither a window entry nor a nonce change on any ancestor.
    function test_ok_failed_spend_is_atomic_across_ancestors() public {
        AgentAccounts.PolicyInput memory rp = _policy(500, 600, 3600);
        bytes32 root = _register(owner, rootSigner, rp, 0);
        _fund(root, 10_000);
        AgentAccounts.PolicyInput memory cp = _policy(100, 600, 3600);
        cp.escalation = esc;
        bytes32 child = _delegate(root, rootSigner, childSigner, cp, 0, 5000);
        _spend(root, rootSigner, 500);
        bytes memory sig = _escSig(child, payee, 200);
        vm.prank(childSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "amount would exceed per_window_max"));
        acc.transfer(child, address(usdc), payee, 200, sig, type(uint64).max);
        assertEq(acc.spentInWindow(child), 0);
        assertEq(acc.spentInWindow(root), 500);
        assertEq(acc.escalationNonce(child), 0);
        assertEq(acc.balanceOf(child, address(usdc)), 5000);
    }
}
