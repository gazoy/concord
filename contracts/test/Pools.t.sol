// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {Pools} from "../src/Pools.sol";
import {MockERC20} from "./MockERC20.sol";

/// Mirrors tests/test_pools.py from the reference.
contract PoolsTest is Test {
    function _epochOf(bytes32 pid_, bytes32 account_) internal view returns (uint64) {
        uint64 e = pools.claimOf(pid_, account_).epoch;
        return e == 0 ? 1 : e; // a not-yet-joined account will get epoch 1 on join
    }

    AgentAccounts acc;
    Pools pools;
    MockERC20 usdc;

    address owner = makeAddr("owner");
    address coordinator = makeAddr("coordinator");
    uint256[] keys;
    address[] signers;
    bytes32[] members;
    bytes32 pid;

    function setUp() public {
        acc = new AgentAccounts();
        pools = new Pools(acc);
        usdc = new MockERC20();
        address[] memory mods = new address[](1);
        mods[0] = address(pools);
        acc.lockModules(mods);
        vm.warp(1_000_000);
        vm.prank(coordinator);
        pid = pools.create(address(usdc), 60, 0);
        for (uint256 i = 0; i < 3; i++) _member(0x700 + i, 1000);
    }

    function _member(uint256 key, uint256 fund) internal returns (bytes32 id) {
        AgentAccounts.PolicyInput memory p;
        p.perTxMax = 1000;
        p.perWindowMax = 1000;
        p.windowSecs = 3600;
        address s = vm.addr(key);
        vm.prank(owner);
        id = acc.register(s, p, key);
        usdc.mint(address(this), fund);
        usdc.approve(address(acc), fund);
        acc.deposit(id, address(usdc), fund);
        keys.push(key);
        signers.push(s);
        members.push(id);
    }

    function _join(uint256 i, uint256 deposit) internal {
        vm.prank(signers[i]);
        pools.join(pid, members[i], deposit, "", 0);
    }

    function _upd(uint256 i, uint64 seq, uint256 balance) internal view returns (Pools.Update memory) {
        bytes32 d = pools.updateDigest(pid, members[i], _epochOf(pid, members[i]), seq, balance);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(keys[i], d);
        return Pools.Update(members[i], seq, balance, abi.encodePacked(r, s, v));
    }

    function test_join_is_a_policy_bounded_commit_to_the_coordinator() public {
        _join(0, 400);
        assertEq(acc.spentInWindow(members[0]), 400);
        assertEq(usdc.balanceOf(address(pools)), 400);
        vm.prank(signers[0]);
        vm.expectRevert(Pools.AlreadyMember.selector);
        pools.join(pid, members[0], 100, "", 0);
        vm.prank(signers[1]);
        vm.expectRevert(AgentAccounts.Unauthorized.selector); // wrong signer for that account
        pools.join(pid, members[2], 100, "", 0);
        // deny the coordinator and joining is refused by the policy
        AgentAccounts.PolicyInput memory p = acc.policyOf(members[1]);
        p.denyList = new address[](1);
        p.denyList[0] = coordinator;
        vm.prank(owner);
        acc.setPolicy(members[1], p);
        vm.prank(signers[1]);
        vm.expectRevert(abi.encodeWithSelector(AgentAccounts.PolicyViolation.selector, "payee is denied"));
        pools.join(pid, members[1], 100, "", 0);
    }

    /// Property: over random rounds of updates, one settlement pays the coordinator exactly the sum
    /// of the members' highest balances; exits return exactly the rest; nothing is created or lost.
    function testFuzz_pool_conservation_and_exit(uint8 rounds, bytes32 seed, bool coordinatorAlive) public {
        rounds = uint8(bound(rounds, 1, 6));
        uint256[3] memory dep = [uint256(300), 500, 1000];
        uint256[3] memory bal;
        uint64 seq = 0;
        for (uint256 i = 0; i < 3; i++) _join(i, dep[i]);
        Pools.Update[] memory batch = new Pools.Update[](3);
        for (uint256 r = 0; r < rounds; r++) {
            seq++;
            for (uint256 i = 0; i < 3; i++) {
                uint256 step = uint256(keccak256(abi.encode(seed, r, i))) % 200;
                bal[i] = bal[i] + step > dep[i] ? dep[i] : bal[i] + step;
                batch[i] = _upd(i, seq, bal[i]);
            }
        }
        uint256 expected = bal[0] + bal[1] + bal[2];
        if (coordinatorAlive) {
            uint256 got = pools.settle(pid, batch);
            assertEq(got, expected);
        } else {
            // coordinator gone: each member exits with its own latest update; anyone could contest
            for (uint256 i = 0; i < 3; i++) {
                vm.prank(signers[i]);
                pools.beginExit(pid, members[i], batch[i].seq, batch[i].balance, batch[i].sig);
            }
            vm.warp(block.timestamp + 60);
            for (uint256 i = 0; i < 3; i++) {
                vm.prank(signers[i]);
                pools.finalizeExit(pid, members[i]);
                assertEq(acc.balanceOf(members[i], address(usdc)), 1000 - bal[i]);
            }
            assertEq(usdc.balanceOf(address(pools)), 0);
        }
        assertEq(usdc.balanceOf(coordinator), expected);
    }

    function test_stale_exit_is_contested() public {
        _join(0, 500);
        Pools.Update memory high = _upd(0, 5, 400);
        // member tries to leave on a low, stale update
        Pools.Update memory low = _upd(0, 1, 50);
        vm.prank(signers[0]);
        pools.beginExit(pid, members[0], low.seq, low.balance, low.sig);
        assertEq(usdc.balanceOf(coordinator), 50);
        // no window yet closed: anyone contests with the higher one
        vm.warp(block.timestamp + 30);
        pools.contestExit(pid, high);
        assertEq(usdc.balanceOf(coordinator), 400);
        // a further stale contest is refused
        Pools.Update memory _pre1 = _upd(0, 3, 300);
        vm.expectRevert(Pools.StaleUpdate.selector);
        pools.contestExit(pid, _pre1);
        vm.warp(block.timestamp + 30);
        // window over: contest refused, exit finalises with the contested balance
        Pools.Update memory _pre2 = _upd(0, 6, 450);
        vm.expectRevert(Pools.NoExitWindow.selector);
        pools.contestExit(pid, _pre2);
        vm.prank(signers[0]);
        pools.finalizeExit(pid, members[0]);
        assertEq(acc.balanceOf(members[0], address(usdc)), 600); // 1000 - 400
        assertTrue(pools.claimOf(pid, members[0]).exited);
    }

    function test_stale_update_in_batch_is_skipped_not_fatal() public {
        _join(0, 500);
        _join(1, 500);
        Pools.Update[] memory b1 = new Pools.Update[](2);
        b1[0] = _upd(0, 2, 100);
        b1[1] = _upd(1, 2, 200);
        assertEq(pools.settle(pid, b1), 300);
        Pools.Update[] memory b2 = new Pools.Update[](2);
        b2[0] = _upd(0, 9, 100); // not above what is settled: skipped (seq is bookkeeping only)
        b2[1] = _upd(1, 3, 250);
        assertEq(pools.settle(pid, b2), 50);
        assertEq(usdc.balanceOf(coordinator), 350);
        Pools.Update[] memory b2b = new Pools.Update[](1);
        b2b[0] = _upd(0, 1, 400); // lower seq, higher balance: applies
        assertEq(pools.settle(pid, b2b), 300);
        // but a forged or out-of-range update fails the batch, as in the reference
        Pools.Update[] memory b3 = new Pools.Update[](1);
        b3[0] = _upd(1, 4, 501);
        vm.expectRevert(abi.encodeWithSelector(Pools.BadUpdate.selector, "balance out of range"));
        pools.settle(pid, b3);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(0x9999, pools.updateDigest(pid, members[1], _epochOf(pid, members[1]), 4, 300));
        b3[0] = Pools.Update(members[1], 4, 300, abi.encodePacked(r, s, v));
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.settle(pid, b3);
    }

    function test_settle_skips_exited_member_and_rejoin_starts_fresh() public {
        _join(0, 500);
        _join(1, 500);
        Pools.Update memory _pre3 = _upd(0, 1, 100);
        vm.prank(signers[0]);
        pools.beginExit(pid, members[0], 1, 100, _pre3.sig);
        vm.warp(block.timestamp + 60);
        vm.prank(signers[0]);
        pools.finalizeExit(pid, members[0]);
        Pools.Update[] memory b = new Pools.Update[](2);
        b[0] = _upd(0, 5, 400); // exited: skipped even though signed and higher
        b[1] = _upd(1, 1, 50);
        assertEq(pools.settle(pid, b), 50);
        assertEq(usdc.balanceOf(coordinator), 150);
        // an exited member may join again with a fresh claim (epoch 2); updates signed under the old
        // membership are not valid against it, whatever their balance (AUDIT-2 A2-2)
        _join(0, 500);
        Pools.Claim memory c = pools.claimOf(pid, members[0]);
        assertEq(c.deposit, 500);
        assertEq(c.paid, 0);
        assertEq(c.epoch, 2);
        Pools.Update[] memory b2 = new Pools.Update[](1);
        b2[0] = b[0]; // epoch-1 signature, balance 400 <= new deposit
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.settle(pid, b2);
        b2[0] = _upd(0, 1, 50); // signed under epoch 2
        assertEq(pools.settle(pid, b2), 50);
    }

    function test_exit_permissions_and_timing() public {
        _join(0, 500);
        vm.prank(signers[1]);
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.beginExit(pid, members[0], 0, 0, "");
        vm.prank(signers[0]);
        vm.expectRevert(Pools.TimeoutNotElapsed.selector);
        pools.finalizeExit(pid, members[0]);
        vm.prank(signers[0]);
        pools.beginExit(pid, members[0], 0, 0, "");
        vm.warp(block.timestamp + 59);
        vm.prank(signers[0]);
        vm.expectRevert(Pools.TimeoutNotElapsed.selector);
        pools.finalizeExit(pid, members[0]);
        vm.warp(block.timestamp + 1);
        vm.prank(coordinator);
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.finalizeExit(pid, members[0]);
        vm.prank(signers[0]);
        pools.finalizeExit(pid, members[0]);
        assertEq(acc.balanceOf(members[0], address(usdc)), 1000);
        assertEq(acc.spentInWindow(members[0]), 500); // exiting does not undo the commitment in the window
        vm.prank(signers[0]);
        vm.expectRevert(Pools.NotMember.selector);
        pools.finalizeExit(pid, members[0]);
    }
}
