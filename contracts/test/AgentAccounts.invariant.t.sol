// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {MockERC20} from "./MockERC20.sol";

/// Random sequences of deposits, spends, delegations, recalls and policy changes over a tree of
/// up to eight accounts. After every call: no money is created or lost, every account's window
/// never exceeds its cap, and the value that left the tree never exceeds what the root allowed.
contract Handler is Test {
    AgentAccounts public acc;
    MockERC20 public usdc;
    address public payee = address(0xBEEF);
    address public owner = address(0xA0);

    bytes32[] public ids;
    mapping(bytes32 => address) public signerOf;
    uint256 public deposited;
    uint256 public left;         // value that reached payee
    uint256 public maxWindowCap; // root's per-window cap (the tree bound)
    mapping(bytes32 => uint256) public capEverHeld; // largest per-window cap an account has had

    constructor(AgentAccounts a, MockERC20 u, bytes32 root, address rootSigner, uint128 rootCap) {
        acc = a;
        usdc = u;
        ids.push(root);
        signerOf[root] = rootSigner;
        maxWindowCap = rootCap;
        capEverHeld[root] = rootCap;
    }

    function count() external view returns (uint256) { return ids.length; }

    function _pick(uint256 seed) internal view returns (bytes32) { return ids[seed % ids.length]; }

    function deposit(uint256 seed, uint96 amount) external {
        amount = uint96(bound(amount, 1, 1_000_000));
        bytes32 id = _pick(seed);
        usdc.mint(address(this), amount);
        usdc.approve(address(acc), amount);
        acc.deposit(id, address(usdc), amount);
        deposited += amount;
    }

    function spend(uint256 seed, uint128 amount) external {
        bytes32 id = _pick(seed);
        amount = uint128(bound(amount, 1, 2 * maxWindowCap));
        vm.prank(signerOf[id]);
        try acc.transfer(id, address(usdc), payee, amount, "") {
            left += amount;
        } catch {}
    }

    function delegateChild(uint256 seed, uint128 txMax, uint128 winMax, uint32 secs, uint96 fund) external {
        if (ids.length >= 8) return;
        bytes32 parent = _pick(seed);
        AgentAccounts.PolicyInput memory p;
        p.perTxMax = txMax;
        p.perWindowMax = winMax;
        p.windowSecs = uint32(bound(secs, 1, 7200));
        address s = address(uint160(0x1000 + ids.length));
        vm.prank(signerOf[parent]);
        try acc.delegate(parent, s, p, seed, address(usdc), fund) returns (bytes32 child) {
            ids.push(child);
            signerOf[child] = s;
            capEverHeld[child] = p.perWindowMax;
        } catch {}
    }

    function recall(uint256 seedA, uint256 seedB, uint96 amount) external {
        bytes32 a = _pick(seedA);
        bytes32 b = _pick(seedB);
        vm.prank(signerOf[a]);
        try acc.recall(a, b, address(usdc), amount) {} catch {}
    }

    function tighten(uint256 seed, uint128 txMax, uint128 winMax) external {
        bytes32 id = _pick(seed);
        AgentAccounts.PolicyInput memory p = acc.policyOf(id);
        p.perTxMax = uint128(bound(txMax, 0, p.perTxMax));
        p.perWindowMax = uint128(bound(winMax, 0, p.perWindowMax));
        vm.prank(owner);
        try acc.setPolicy(id, p) {} catch {}
    }

    function warp(uint32 secs) external {
        vm.warp(block.timestamp + bound(secs, 0, 600));
    }
}

contract AgentAccountsInvariant is Test {
    AgentAccounts acc;
    MockERC20 usdc;
    Handler h;
    bytes32 root;
    uint128 constant ROOT_CAP = 5000;

    function setUp() public {
        acc = new AgentAccounts();
        usdc = new MockERC20();
        vm.warp(1_000_000);
        AgentAccounts.PolicyInput memory p;
        p.perTxMax = ROOT_CAP;
        p.perWindowMax = ROOT_CAP;
        p.windowSecs = 3600;
        vm.prank(address(0xA0));
        root = acc.register(address(0x1000), p, 0);
        h = new Handler(acc, usdc, root, address(0x1000), ROOT_CAP);
        targetContract(address(h));
    }

    /// Money: contract holds exactly the sum of internal balances; deposits = balances + what left.
    function invariant_conservation() public view {
        uint256 sum;
        for (uint256 i = 0; i < h.count(); i++) sum += acc.balanceOf(h.ids(i), address(usdc));
        assertEq(usdc.balanceOf(address(acc)), sum, "contract balance != sum of accounts");
        assertEq(h.deposited(), sum + h.left(), "money created or lost");
        assertEq(usdc.balanceOf(h.payee()), h.left(), "payee received != recorded");
    }

    /// No account's window ever exceeds the largest cap it has held (tightening a policy does not
    /// undo past spends, in the reference or here; it binds the next one).
    function invariant_windows_within_caps() public view {
        for (uint256 i = 0; i < h.count(); i++) {
            bytes32 id = h.ids(i);
            assertLe(acc.spentInWindow(id), h.capEverHeld(id), "window exceeds cap ever held");
        }
        assertLe(acc.spentInWindow(root), ROOT_CAP, "root window exceeds root cap");
    }

    /// A descendant's window never shows more than its ancestor's (the ancestor recorded everything
    /// the descendant did, over a window at least as long is not guaranteed, so compare only when
    /// the ancestor's window is the longer one).
    function invariant_ancestor_sees_descendant_spend() public view {
        for (uint256 i = 0; i < h.count(); i++) {
            bytes32 id = h.ids(i);
            bytes32 p = acc.parentOf(id);
            if (p == bytes32(0)) continue;
            if (acc.policyOf(p).windowSecs >= acc.policyOf(id).windowSecs) {
                assertLe(acc.spentInWindow(id), acc.spentInWindow(p), "child window > parent window");
            }
        }
    }
}
