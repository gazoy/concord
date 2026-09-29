// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {MockERC20} from "./MockERC20.sol";

/// Mirrors tests/test_tree.py from the Python reference, plus EVM-specific cases.
contract AgentAccountsTest is Test {
    AgentAccounts acc;
    MockERC20 usdc;

    address owner = makeAddr("owner");
    address rootSigner = makeAddr("rootSigner");
    address childSigner = makeAddr("childSigner");
    address grandSigner = makeAddr("grandSigner");
    address payee = makeAddr("payee");
    address other = makeAddr("other");
    uint256 escKey = 0xA11CE;
    address esc;

    bytes32 root;

    function setUp() public {
        acc = new AgentAccounts();
        usdc = new MockERC20();
        esc = vm.addr(escKey);
        vm.warp(1_000_000);
        root = _register(owner, rootSigner, _policy(500, 1000, 3600), 0);
        _fund(root, 10_000);
    }

    // ------------------------------------------------------------ helpers

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

    function _delegate(bytes32 parent, address signer, address s, AgentAccounts.PolicyInput memory p, uint256 fund)
        internal
        returns (bytes32)
    {
        vm.prank(signer);
        return acc.delegate(parent, s, p, 0, address(usdc), fund);
    }

    function _spend(bytes32 id, address signer, uint256 amount) internal {
        vm.prank(signer);
        acc.transfer(id, address(usdc), payee, amount, "");
    }

    function _escSig(bytes32 id, uint256 amount) internal view returns (bytes memory) {
        bytes32 digest = acc.escalationDigest(id, address(usdc), payee, amount, acc.escalationNonce(id));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(escKey, digest);
        return abi.encodePacked(r, s, v);
    }

    // ------------------------------------------------------------ tests

    function test_child_policy_must_sit_within_parent() public {
        vm.startPrank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.BadPolicy.selector, "child per_tx_max exceeds parent"));
        acc.delegate(root, childSigner, _policy(501, 1000, 3600), 0, address(usdc), 0);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.BadPolicy.selector, "child per_window_max exceeds parent"));
        acc.delegate(root, childSigner, _policy(500, 1001, 3600), 0, address(usdc), 0);
        // shorter window and its own co-signer are allowed
        AgentAccounts.PolicyInput memory p = _policy(500, 1000, 60);
        p.escalation = esc;
        acc.delegate(root, childSigner, p, 0, address(usdc), 0);
        vm.stopPrank();
    }

    function test_deny_list_inherits_and_allow_list_is_subset() public {
        AgentAccounts.PolicyInput memory pp = _policy(500, 1000, 3600);
        pp.denyList = new address[](1);
        pp.denyList[0] = other;
        pp.hasAllowList = true;
        pp.allowList = new address[](1);
        pp.allowList[0] = payee;
        bytes32 r2 = _register(owner, rootSigner, pp, 1);

        vm.startPrank(rootSigner);
        AgentAccounts.PolicyInput memory cp = _policy(100, 100, 3600);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.BadPolicy.selector, "child allow list must be a subset of the parent's"));
        acc.delegate(r2, childSigner, cp, 0, address(usdc), 0); // no allow list while parent has one

        cp.hasAllowList = true;
        cp.allowList = new address[](1);
        cp.allowList[0] = payee;
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.BadPolicy.selector, "child deny list must include the parent's"));
        acc.delegate(r2, childSigner, cp, 0, address(usdc), 0); // parent's deny list not inherited

        cp.denyList = pp.denyList;
        cp.allowList[0] = other;
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.BadPolicy.selector, "child allow list must be a subset of the parent's"));
        acc.delegate(r2, childSigner, cp, 0, address(usdc), 0);

        cp.allowList[0] = payee;
        acc.delegate(r2, childSigner, cp, 0, address(usdc), 0);
        vm.stopPrank();
    }

    function test_funding_and_recall_are_not_spends() public {
        bytes32 child = _delegate(root, rootSigner, childSigner, _policy(100, 300, 3600), 5000);
        assertEq(acc.balanceOf(child, address(usdc)), 5000);
        assertEq(acc.balanceOf(root, address(usdc)), 5000);
        assertEq(acc.spentInWindow(root), 0);
        vm.prank(rootSigner);
        acc.recall(root, child, address(usdc), 0);
        assertEq(acc.balanceOf(child, address(usdc)), 0);
        assertEq(acc.balanceOf(root, address(usdc)), 10_000);
        assertEq(acc.spentInWindow(root), 0);
        assertEq(acc.spentInWindow(child), 0);
    }

    function test_spend_counts_against_every_ancestor() public {
        bytes32 child = _delegate(root, rootSigner, childSigner, _policy(500, 1000, 3600), 5000);
        bytes32 grand = _delegate(child, childSigner, grandSigner, _policy(500, 1000, 3600), 2000);
        _spend(grand, grandSigner, 400);
        assertEq(acc.spentInWindow(grand), 400);
        assertEq(acc.spentInWindow(child), 400);
        assertEq(acc.spentInWindow(root), 400);
        assertEq(usdc.balanceOf(payee), 400);
        // the root's window is what binds the grandchild, whatever its own policy says
        _spend(root, rootSigner, 500);
        vm.prank(grandSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "amount would exceed per_window_max"));
        acc.transfer(grand, address(usdc), payee, 200, "");
        // and nothing was recorded by the refused spend
        assertEq(acc.spentInWindow(grand), 400);
        assertEq(acc.spentInWindow(root), 900);
    }

    function test_tightened_parent_binds_existing_children() public {
        bytes32 child = _delegate(root, rootSigner, childSigner, _policy(500, 1000, 3600), 5000);
        vm.prank(owner);
        acc.setPolicy(root, _policy(50, 1000, 3600));
        vm.prank(childSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "amount exceeds per_tx_max"));
        acc.transfer(child, address(usdc), payee, 100, "");
        _spend(child, childSigner, 50);
    }

    function test_ancestor_signer_administers_descendants() public {
        bytes32 child = _delegate(root, rootSigner, childSigner, _policy(500, 1000, 3600), 5000);
        bytes32 grand = _delegate(child, childSigner, grandSigner, _policy(500, 1000, 3600), 1000);
        // root's signer may set the grandchild's policy and rotate its signer
        vm.prank(rootSigner);
        acc.setPolicy(grand, _policy(10, 10, 3600));
        vm.prank(rootSigner);
        acc.rotateSigner(grand, other);
        assertEq(acc.signerOf(grand), other);
        // a sibling or stranger may not
        bytes32 child2 = _delegate(root, rootSigner, other, _policy(500, 1000, 3600), 0);
        assertTrue(child2 != bytes32(0));
        vm.prank(grandSigner); // no longer the signer, not an ancestor
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.setPolicy(grand, _policy(10, 10, 3600));
        vm.prank(other); // child2's signer is not an ancestor of grand
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.setPolicy(child, _policy(10, 10, 3600));
        // a child's policy set by an ancestor must still sit within the parent's
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.BadPolicy.selector, "child per_tx_max exceeds parent"));
        acc.setPolicy(grand, _policy(501, 10, 3600));
    }

    function test_only_signer_spends_delegates_recalls() public {
        bytes32 child = _delegate(root, rootSigner, childSigner, _policy(500, 1000, 3600), 5000);
        vm.prank(owner);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.transfer(root, address(usdc), payee, 1, "");
        vm.prank(childSigner);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.delegate(root, other, _policy(1, 1, 1), 0, address(usdc), 0);
        vm.prank(childSigner);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.recall(root, child, address(usdc), 0);
        // recall only from a descendant
        bytes32 r2 = _register(owner, other, _policy(1, 1, 1), 7);
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.NotDescendant.selector);
        acc.recall(root, r2, address(usdc), 0);
    }

    function test_escalation_lifts_only_the_signing_account() public {
        AgentAccounts.PolicyInput memory cp = _policy(100, 1000, 3600);
        cp.escalation = esc;
        bytes32 child = _delegate(root, rootSigner, childSigner, cp, 5000);
        // 300 > child per_tx_max 100, allowed with the co-signature; root's cap is 500 so it passes
        bytes memory sig300 = _escSig(child, 300);
        vm.prank(childSigner);
        acc.transfer(child, address(usdc), payee, 300, sig300);
        assertEq(usdc.balanceOf(payee), 300);
        assertEq(acc.escalationNonce(child), 1);
        // 600 > root per_tx_max 500: the child's co-signer cannot lift an ancestor's cap
        bytes memory sig600 = _escSig(child, 600);
        vm.prank(childSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "amount exceeds per_tx_max"));
        acc.transfer(child, address(usdc), payee, 600, sig600);
        assertEq(acc.escalationNonce(child), 1); // failed spend consumed nothing
    }

    function test_escalation_signature_is_bound_and_single_use() public {
        AgentAccounts.PolicyInput memory cp = _policy(100, 1000, 3600);
        cp.escalation = esc;
        bytes32 child = _delegate(root, rootSigner, childSigner, cp, 5000);
        bytes memory sig = _escSig(child, 300);
        vm.prank(childSigner);
        vm.expectRevert(AgentAccounts.BadEscalation.selector);
        acc.transfer(child, address(usdc), payee, 301, sig); // different amount
        vm.prank(childSigner);
        vm.expectRevert(AgentAccounts.BadEscalation.selector);
        acc.transfer(child, address(usdc), other, 300, sig); // different payee
        vm.prank(childSigner);
        acc.transfer(child, address(usdc), payee, 300, sig);
        vm.prank(childSigner);
        vm.expectRevert(AgentAccounts.BadEscalation.selector);
        acc.transfer(child, address(usdc), payee, 300, sig); // replay
        // no co-signer on the policy: any signature is refused
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.BadEscalation.selector);
        acc.transfer(root, address(usdc), payee, 300, sig);
    }

    function test_validation_precedes_recording() public {
        // insufficient funds must not leave a window entry on any ancestor
        bytes32 child = _delegate(root, rootSigner, childSigner, _policy(500, 1000, 3600), 100);
        vm.prank(childSigner);
        vm.expectRevert(AgentAccounts.InsufficientFunds.selector);
        acc.transfer(child, address(usdc), payee, 200, "");
        assertEq(acc.spentInWindow(child), 0);
        assertEq(acc.spentInWindow(root), 0);
    }

    function test_zero_amounts_refused_and_no_money_created() public {
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.ZeroAmount.selector);
        acc.transfer(root, address(usdc), payee, 0, "");
        vm.expectRevert(AgentAccounts.ZeroAmount.selector);
        acc.deposit(root, address(usdc), 0);
        bytes32 child = _delegate(root, rootSigner, childSigner, _policy(500, 1000, 3600), 0);
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.InsufficientFunds.selector);
        acc.delegate(root, other, _policy(1, 1, 1), 1, address(usdc), 10_001);
        vm.prank(rootSigner);
        vm.expectRevert(AgentAccounts.InsufficientFunds.selector);
        acc.recall(root, child, address(usdc), 1);
    }

    function test_window_rolls() public {
        _spend(root, rootSigner, 500);
        _spend(root, rootSigner, 500);
        vm.prank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "amount would exceed per_window_max"));
        acc.transfer(root, address(usdc), payee, 1, "");
        vm.warp(block.timestamp + 3600); // entries at t are counted while t > now - window; at exactly the edge they drop
        assertEq(acc.spentInWindow(root), 0);
        _spend(root, rootSigner, 500);
    }

    function test_expiry_and_lists() public {
        AgentAccounts.PolicyInput memory p = _policy(500, 1000, 3600);
        p.expiry = uint64(block.timestamp + 10);
        p.denyList = new address[](1);
        p.denyList[0] = other;
        bytes32 r2 = _register(owner, rootSigner, p, 9);
        _fund(r2, 1000);
        vm.prank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "payee is denied"));
        acc.transfer(r2, address(usdc), other, 1, "");
        vm.warp(block.timestamp + 10);
        vm.prank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "policy expired"));
        acc.transfer(r2, address(usdc), payee, 1, "");
    }

    function test_child_expiry_bounded_by_parent() public {
        AgentAccounts.PolicyInput memory p = _policy(500, 1000, 3600);
        p.expiry = uint64(block.timestamp + 100);
        bytes32 r2 = _register(owner, rootSigner, p, 11);
        vm.startPrank(rootSigner);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.BadPolicy.selector, "child expiry must not be later than the parent's"));
        acc.delegate(r2, childSigner, _policy(1, 1, 1), 0, address(usdc), 0); // no expiry while parent has one
        AgentAccounts.PolicyInput memory c = _policy(1, 1, 1);
        c.expiry = uint64(block.timestamp + 101);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.BadPolicy.selector, "child expiry must not be later than the parent's"));
        acc.delegate(r2, childSigner, c, 0, address(usdc), 0);
        c.expiry = uint64(block.timestamp + 100);
        acc.delegate(r2, childSigner, c, 0, address(usdc), 0);
        vm.stopPrank();
    }

    function test_modules_lock_once_and_only_modules_commit() public {
        address[] memory mods = new address[](1);
        mods[0] = other;
        vm.prank(owner);
        vm.expectRevert(AgentAccounts.ModulesAlreadyLocked.selector);
        acc.lockModules(mods);
        acc.lockModules(mods); // deployer is this test contract
        vm.expectRevert(AgentAccounts.ModulesAlreadyLocked.selector);
        acc.lockModules(mods);
        vm.prank(payee);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.commit(root, rootSigner, address(usdc), payee, 1, "");
        // a module may commit only on behalf of the signer, under the policy
        vm.prank(other);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.commit(root, owner, address(usdc), payee, 1, "");
        vm.prank(other);
        acc.commit(root, rootSigner, address(usdc), payee, 400, "");
        assertEq(usdc.balanceOf(other), 400);
        assertEq(acc.spentInWindow(root), 400);
        // refund returns escrow without touching the window
        vm.startPrank(other);
        usdc.approve(address(acc), 400);
        acc.refund(root, address(usdc), 400);
        vm.stopPrank();
        assertEq(acc.balanceOf(root, address(usdc)), 10_000);
        assertEq(acc.spentInWindow(root), 400);
    }

    /// Property (mirrors test_no_branch_exceeds_any_ancestor): whatever the caps below, the root's
    /// window cap bounds the total that leaves the tree.
    function testFuzz_no_branch_exceeds_root(uint128 rootCap, uint128 c1, uint128 c2, uint8 n, uint64 seed) public {
        rootCap = uint128(bound(rootCap, 1, 5000));
        c1 = uint128(bound(c1, 1, 10_000));
        c2 = uint128(bound(c2, 1, 10_000));
        n = uint8(bound(n, 1, 20));
        bytes32 r = _register(owner, rootSigner, _policy(rootCap, rootCap, 3600), 1000 + uint256(seed));
        _fund(r, 100_000);
        // children may claim wider caps only up to the root's; clamp like the reference's `within`
        uint128 k1 = c1 > rootCap ? rootCap : c1;
        uint128 k2 = c2 > rootCap ? rootCap : c2;
        bytes32 a = _delegate(r, rootSigner, childSigner, _policy(k1, k1, 3600), 40_000);
        bytes32 b = _delegate(r, rootSigner, grandSigner, _policy(k2, k2, 3600), 40_000);
        uint256 left = 0;
        for (uint256 i = 0; i < n; i++) {
            uint256 amt = (uint256(keccak256(abi.encode(seed, i))) % rootCap) + 1;
            (bytes32 who, address s) = i % 2 == 0 ? (a, childSigner) : (b, grandSigner);
            vm.prank(s);
            (bool ok,) = address(acc).call(
                abi.encodeWithSelector(acc.transfer.selector, who, address(usdc), payee, amt, bytes(""))
            );
            if (ok) left += amt;
        }
        assertLe(left, rootCap);
        assertEq(acc.spentInWindow(r), left);
        assertEq(acc.spentInWindow(a) + acc.spentInWindow(b), left);
    }
}
