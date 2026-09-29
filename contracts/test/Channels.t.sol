// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {PaymentChannels} from "../src/PaymentChannels.sol";
import {MockERC20} from "./MockERC20.sol";

/// Mirrors tests/test_channels.py from the reference (streams excepted).
contract ChannelsTest is Test {
    AgentAccounts acc;
    PaymentChannels ch;
    MockERC20 usdc;

    address owner = makeAddr("owner");
    uint256 signerKey = 0x5161;
    address signer;
    uint256 strangerKey = 0x5741;
    address payee = makeAddr("payee");
    bytes32 root;

    function setUp() public {
        acc = new AgentAccounts();
        ch = new PaymentChannels(acc);
        usdc = new MockERC20();
        signer = vm.addr(signerKey);
        address[] memory mods = new address[](1);
        mods[0] = address(ch);
        acc.lockModules(mods);
        vm.warp(1_000_000);
        AgentAccounts.PolicyInput memory p;
        p.perTxMax = 1000;
        p.perWindowMax = 2000;
        p.windowSecs = 3600;
        vm.prank(owner);
        root = acc.register(signer, p, 0);
        usdc.mint(address(this), 10_000);
        usdc.approve(address(acc), 10_000);
        acc.deposit(root, address(usdc), 10_000);
    }

    function _open(uint256 deposit, uint64 timeout) internal returns (bytes32 id) {
        vm.prank(signer);
        id = ch.open(root, payee, address(usdc), deposit, timeout, 0, "", 0);
    }

    function _sig(uint256 key, bytes32 id, uint64 seq, uint256 balance) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, ch.updateDigest(id, root, seq, balance));
        return abi.encodePacked(r, s, v);
    }

    function test_open_is_a_policy_bounded_commit() public {
        bytes32 id = _open(600, 60);
        assertEq(usdc.balanceOf(address(ch)), 600);
        assertEq(acc.balanceOf(root, address(usdc)), 9400);
        assertEq(acc.spentInWindow(root), 600);
        // per_tx_max 1000: a 1001 deposit is refused by the policy and nothing moves
        vm.prank(signer);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "amount exceeds per_tx_max"));
        ch.open(root, payee, address(usdc), 1001, 60, 1, "", 0);
        // only the signer may open
        vm.prank(owner);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        ch.open(root, payee, address(usdc), 100, 60, 2, "", 0);
        // duplicate id
        vm.prank(signer);
        vm.expectRevert(PaymentChannels.ChannelExists.selector);
        ch.open(root, payee, address(usdc), 100, 60, 0, "", 0);
        assertTrue(id != bytes32(0));
    }

    /// Property: whatever the sequence of updates, the payee receives exactly the highest balance
    /// applied, the payer gets the rest back, and nothing is created or lost.
    function testFuzz_channel_conservation_and_bounds(uint16 depositRaw, uint8 n, bytes32 seed, bool payeeContests) public {
        uint256 deposit = bound(depositRaw, 1, 1000);
        n = uint8(bound(n, 1, 12));
        bytes32 id = _open(deposit, 100);
        uint256 balance = 0;
        uint256 highest = 0;
        uint64 seq = 0;
        bytes memory lastSig;
        uint64 lastSeq;
        uint256 lastBal;
        for (uint256 i = 0; i < n; i++) {
            uint256 step = uint256(keccak256(abi.encode(seed, i))) % (deposit / 2 + 2);
            balance = balance + step > deposit ? deposit : balance + step;
            seq++;
            bytes memory sig = _sig(signerKey, id, seq, balance);
            if (i % 3 == 0) {
                // payee settles some updates as they arrive
                ch.settle(id, seq, balance, sig);
                highest = balance;
            }
            lastSig = sig;
            lastSeq = seq;
            lastBal = balance;
        }
        // payer closes with a possibly stale update (the one it last settled) or none
        vm.prank(signer);
        ch.beginClose(id, 0, 0, "");
        if (payeeContests) {
            if (lastSeq > highest_seq(id)) {
                ch.settle(id, lastSeq, lastBal, lastSig);
                highest = lastBal;
            }
        }
        vm.warp(block.timestamp + 100);
        vm.prank(signer);
        ch.finalizeClose(id);
        assertEq(usdc.balanceOf(payee), highest);
        assertEq(acc.balanceOf(root, address(usdc)), 10_000 - highest);
        assertEq(usdc.balanceOf(address(ch)), 0);
        assertLe(highest, deposit);
    }

    function highest_seq(bytes32 id) internal view returns (uint64) { return ch.get(id).seq; }

    function test_stale_and_forged_updates_rejected() public {
        bytes32 id = _open(500, 60);
        ch.settle(id, 2, 200, _sig(signerKey, id, 2, 200));
        assertEq(usdc.balanceOf(payee), 200);
        // stale seq
        bytes memory _pre1 = _sig(signerKey, id, 2, 300);
        vm.expectRevert(PaymentChannels.StaleUpdate.selector);
        ch.settle(id, 2, 300, _pre1);
        bytes memory _pre2 = _sig(signerKey, id, 1, 300);
        vm.expectRevert(PaymentChannels.StaleUpdate.selector);
        ch.settle(id, 1, 300, _pre2);
        // decreasing balance
        bytes memory _pre3 = _sig(signerKey, id, 3, 100);
        vm.expectRevert(abi.encodeWithSelector(PaymentChannels.BadUpdate.selector, "balance may not decrease"));
        ch.settle(id, 3, 100, _pre3);
        // over deposit
        bytes memory _pre4 = _sig(signerKey, id, 3, 501);
        vm.expectRevert(abi.encodeWithSelector(PaymentChannels.BadUpdate.selector, "balance exceeds deposit"));
        ch.settle(id, 3, 501, _pre4);
        // forged by a stranger
        bytes memory _pre5 = _sig(strangerKey, id, 3, 300);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id, 3, 300, _pre5);
        // signature for a different channel id / seq / balance does not transfer
        bytes memory sig = _sig(signerKey, id, 3, 300);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id, 3, 301, sig);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id, 4, 300, sig);
        ch.settle(id, 3, 300, sig);
        assertEq(usdc.balanceOf(payee), 300);
    }

    function test_close_cannot_be_finalised_early() public {
        bytes32 id = _open(500, 60);
        vm.prank(signer);
        vm.expectRevert(PaymentChannels.NotClosing.selector);
        ch.finalizeClose(id);
        bytes memory _pre6 = _sig(signerKey, id, 1, 100);
        vm.prank(signer);
        ch.beginClose(id, 1, 100, _pre6);
        assertEq(usdc.balanceOf(payee), 100);
        vm.warp(block.timestamp + 59);
        vm.prank(signer);
        vm.expectRevert(PaymentChannels.TimeoutNotElapsed.selector);
        ch.finalizeClose(id);
        // only the payer's signer closes and finalises
        vm.warp(block.timestamp + 1);
        vm.prank(payee);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.finalizeClose(id);
        vm.prank(signer);
        ch.finalizeClose(id);
        assertEq(acc.balanceOf(root, address(usdc)), 9900);
        assertEq(acc.spentInWindow(root), 500); // the refund is not a spend, and the window is unchanged
        // closed channels accept nothing
        bytes memory _pre7 = _sig(signerKey, id, 2, 200);
        vm.expectRevert(PaymentChannels.ChannelClosed.selector);
        ch.settle(id, 2, 200, _pre7);
        vm.prank(signer);
        vm.expectRevert(PaymentChannels.ChannelClosed.selector);
        ch.finalizeClose(id);
    }

    function test_payee_wins_close_with_higher_update() public {
        bytes32 id = _open(500, 60);
        bytes memory u5 = _sig(signerKey, id, 5, 450);
        // payer tries to close on a stale, lower update
        bytes memory _pre8 = _sig(signerKey, id, 2, 100);
        vm.prank(signer);
        ch.beginClose(id, 2, 100, _pre8);
        // payee submits the higher one inside the window
        vm.warp(block.timestamp + 30);
        ch.settle(id, 5, 450, u5);
        vm.warp(block.timestamp + 30);
        vm.prank(signer);
        ch.finalizeClose(id);
        assertEq(usdc.balanceOf(payee), 450);
        assertEq(acc.balanceOf(root, address(usdc)), 9550);
    }

    function test_signer_rotation_invalidates_old_updates() public {
        bytes32 id = _open(500, 60);
        bytes memory old = _sig(signerKey, id, 1, 100);
        vm.prank(owner);
        acc.rotateSigner(root, vm.addr(strangerKey));
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id, 1, 100, old);
        ch.settle(id, 1, 100, _sig(strangerKey, id, 1, 100)); // the new signer's updates are the valid ones
        // and the new signer, not the old, controls closing
        vm.prank(signer);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.beginClose(id, 0, 0, "");
    }

    function test_policy_allow_list_blocks_open() public {
        AgentAccounts.PolicyInput memory p = acc.policyOf(root);
        p.hasAllowList = true;
        p.allowList = new address[](1);
        p.allowList[0] = makeAddr("someone-else");
        vm.prank(owner);
        acc.setPolicy(root, p);
        vm.prank(signer);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "payee is not on the allow list"));
        ch.open(root, payee, address(usdc), 100, 60, 9, "", 0);
    }
}
