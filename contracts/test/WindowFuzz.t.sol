// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {MockERC20} from "./MockERC20.sol";

/// The bucketed on-chain window against the reference's exact entry list (SpendWindow in
/// foliant/accounts.py): over random spend times, amounts and window changes, the contract must
/// never count LESS than the reference (so it can never let more than the cap leave in any
/// windowSecs interval), and never MORE than the reference plus what the reference counted in the
/// preceding bucketLen seconds (the documented over-count bound).
contract WindowFuzzTest is Test {
    AgentAccounts acc;
    MockERC20 usdc;
    address owner = makeAddr("owner");
    address signer = makeAddr("signer");
    address payee = makeAddr("payee");

    uint64[] ts;      // the reference's live entries (pruned like SpendWindow)
    uint256[] amt;
    uint64[] allTs;   // everything ever recorded, for the upper bound
    uint256[] allAmt;

    function _policy(uint32 w) internal pure returns (AgentAccounts.PolicyInput memory p) {
        p.perTxMax = type(uint128).max;
        p.perWindowMax = type(uint128).max; // the cap is not under test here; the count is
        p.windowSecs = w;
    }

    /// SpendWindow.spent from the reference: entries at or before the cutoff are dropped for good
    /// (so lengthening a window later does not resurrect them), the rest are summed.
    function _exact(uint256 now_, uint256 w) internal returns (uint256 total) {
        uint256 cutoff = now_ > w ? now_ - w : 0;
        uint256 k = 0;
        for (uint256 i = 0; i < ts.length; i++) {
            if (ts[i] > cutoff) {
                ts[k] = ts[i];
                amt[k] = amt[i];
                k++;
                total += amt[i];
            }
        }
        while (ts.length > k) { ts.pop(); amt.pop(); }
    }

    /// Everything ever recorded after `lo`: the most the bucketing could still be holding.
    function _allAfter(uint256 lo) internal view returns (uint256 total) {
        for (uint256 i = 0; i < allTs.length; i++) if (allTs[i] > lo) total += allAmt[i];
    }

    function testFuzz_bucketed_window_bounds_reference(uint32 w0, uint32 w1, uint8 n, bytes32 seed) public {
        uint32 w = uint32(bound(w0, 1, 30 days));
        uint32 wAlt = uint32(bound(w1, 1, 30 days));
        uint256 wMax = w > wAlt ? w : wAlt; // largest window ever in force
        n = uint8(bound(n, 1, 40));
        acc = new AgentAccounts();
        usdc = new MockERC20();
        vm.warp(1_000_000);
        vm.prank(owner);
        bytes32 id = acc.register(signer, _policy(w), 0);
        usdc.mint(address(this), 1e30);
        usdc.approve(address(acc), 1e30);
        acc.deposit(id, address(usdc), 1e30);

        uint256 t = block.timestamp;
        uint256 changes = 0;
        for (uint256 i = 0; i < n; i++) {
            bytes32 r = keccak256(abi.encode(seed, i));
            // steps concentrated around bucket edges and the window length
            uint256 step = uint256(r) % 4 == 0 ? uint256(w) : (uint256(r) % (uint256(w) / 8 + 2));
            t += step;
            vm.warp(t);
            if (uint256(r) % 7 == 0) {
                // administrator changes the window mid-sequence (both directions)
                uint32 nw = (i % 2 == 0) ? wAlt : w;
                vm.prank(owner);
                acc.setPolicy(id, _policy(nw));
                if (nw != w) changes++;
                w = nw;
            }
            uint256 a = (uint256(r) >> 8) % 1000 + 1;
            vm.prank(signer);
            acc.transfer(id, address(usdc), payee, a, "", 0);
            ts.push(uint64(t));
            amt.push(a);
            allTs.push(uint64(t));
            allAmt.push(a);

            uint256 got = acc.spentInWindow(id);
            uint256 exact = _exact(t, w);
            // safety: never less than the reference, so no more than the cap can leave in any window
            assertGe(got, exact, "under-count: the chain would let more than the cap leave");
            // conservativeness: with a fixed window, never more than everything recorded inside the
            // window plus one bucket; each administrator window change can extend a live bucket by at
            // most one further window plus a bucket (documented in the contract header)
            uint256 mb = (wMax + 30) / 31;
            uint256 span = (uint256(w) + mb) + changes * (wMax + mb);
            uint256 lo = t > span ? t - span : 0;
            assertLe(got, _allAfter(lo), "over-count beyond the documented bound");
        }
    }
}
