// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {MockERC20} from "./MockERC20.sol";

/// The reviewer's reproductions for docs/spec/REVIEW-1.md, kept as a record of contract behaviour
/// the specification had to be written around. None is a contract bug; each comment says how the
/// draft was corrected in response (the contract is deployed and unchanged).
contract AuditSpecTest is Test {
    AgentAccounts acc;
    MockERC20 token;
    address signer = makeAddr("signer");
    address payee = makeAddr("payee");

    function setUp() public {
        acc = new AgentAccounts();
        token = new MockERC20();
        vm.warp(1_000_000);
    }

    function _reg(AgentAccounts.PolicyInput memory p) internal returns (bytes32 id) {
        id = acc.register(signer, p, 0);
        token.mint(address(this), 1e30);
        token.approve(address(acc), 1e30);
        acc.deposit(id, address(token), 1e30);
    }

    function _wide(uint32 w) internal pure returns (AgentAccounts.PolicyInput memory p) {
        p.perTxMax = type(uint128).max;
        p.perWindowMax = type(uint128).max;
        p.windowSecs = w;
    }

    /// S-2: the contract encodes "never" as expiry 0. The spec now forbids 0 on the wire (null means
    /// never) and makes rejecting it the decoder's job; the Python reference rejects it, and the
    /// zero-address co-signer, at load.
    function test_S2_expiry_zero_is_never_on_chain() public {
        AgentAccounts.PolicyInput memory p = _wide(60);
        p.expiry = 0;
        bytes32 id = _reg(p);
        vm.warp(type(uint64).max - 1);
        vm.prank(signer);
        acc.transfer(id, address(token), payee, 1, "", 0); // ok; Python: PolicyViolation("expired")
    }

    /// S-3: the contract accepts any uint32 windowSecs; the spec's claim of a 30-day bound was wrong
    /// and now names the Python reference as the one that bounds it.
    function test_S3_no_30_day_bound_on_windowSecs() public {
        AgentAccounts.PolicyInput memory p = _wide(type(uint32).max); // ~136 years
        bytes32 id = _reg(p);
        assertEq(acc.policyOf(id).windowSecs, type(uint32).max);
    }

    /// S-1: a slot that aged out under W=60 but was not reused is counted again once windowSecs
    /// grows. Under the corrected §6.1 the exact figure here is 0 (the spend aged out under a window
    /// in force), and counting it is the over-count §6.2 permits; the contract never counts less.
    function test_S1_lengthening_window_resurrects_aged_out_spend() public {
        bytes32 id = _reg(_wide(60));
        vm.prank(signer);
        acc.transfer(id, address(token), payee, 100, "", 0);          // t = 1_000_000
        vm.warp(1_000_000 + 200);
        assertEq(acc.spentInWindow(id), 0, "aged out under W=60");
        acc.setPolicy(id, _wide(3600));                              // owner lengthens
        assertEq(acc.spentInWindow(id), 100, "resurrected");        // §6.1 text says 0
    }

    /// S-14: an unchecked uint128() cast truncates silently; the SpecVectors runner now checks the
    /// range before narrowing and treats an unrepresentable value as the decoder's rejection.
    function test_S14_runner_cast_truncates_silently() public pure {
        uint256 big = 340282366920938463463374607431768211456; // 2^128
        assertEq(uint128(big), 0);
    }

    /// S-7: an account's own signer cannot set its own policy. §4.4 now says so, and tree vectors
    /// name "owner" explicitly when the owner administers a root.
    function test_S7_own_signer_cannot_set_own_policy() public {
        bytes32 id = _reg(_wide(60));
        vm.prank(signer);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.setPolicy(id, _wide(120));
    }
}
