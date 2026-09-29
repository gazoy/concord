// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {PaymentChannels} from "../src/PaymentChannels.sol";
import {Pools} from "../src/Pools.sol";
import {MockERC20} from "./MockERC20.sol";

/// All three contracts under random sequences: deposits, tree delegation, channel open/settle/close,
/// pool join/settle/exit/contest, with valid and forged updates. After every call:
///  - the token is conserved across accounts, channel escrow, pool escrow, payees and coordinator;
///  - no payee or coordinator has received more than the deposits committed to it;
///  - the root's spend window bounds everything committed out of the tree;
///  - closed channels and exited claims hold nothing.
contract SystemHandler is Test {
    AgentAccounts public acc;
    PaymentChannels public ch;
    Pools public pools;
    MockERC20 public usdc;

    address public owner = address(0xA0);
    address public payee = address(0xBEEF);
    address public coordinator = address(0xC0DE);
    bytes32 public pid;

    bytes32[] public ids;
    uint256[] public keys;
    bytes32[] public channels;
    uint256 public deposited;
    uint256 public committedToPayee;      // channel deposits (upper bound on what payee can get)
    uint256 public committedToCoordinator;

    constructor(AgentAccounts a, PaymentChannels c, Pools p, MockERC20 u, bytes32 root, uint256 rootKey) {
        acc = a; ch = c; pools = p; usdc = u;
        ids.push(root);
        keys.push(rootKey);
        vm.prank(coordinator);
        pid = pools.create(address(u), 30, 0);
    }

    function count() external view returns (uint256) { return ids.length; }
    function channelCount() external view returns (uint256) { return channels.length; }

    function _pick(uint256 seed) internal view returns (uint256) { return seed % ids.length; }

    function deposit(uint256 seed, uint96 amount) external {
        amount = uint96(bound(amount, 1, 100_000));
        usdc.mint(address(this), amount);
        usdc.approve(address(acc), amount);
        acc.deposit(ids[_pick(seed)], address(usdc), amount);
        deposited += amount;
    }

    function delegateChild(uint256 seed, uint128 cap, uint96 fund) external {
        if (ids.length >= 6) return;
        uint256 i = _pick(seed);
        AgentAccounts.PolicyInput memory p;
        p.perTxMax = cap;
        p.perWindowMax = cap;
        p.windowSecs = 600;
        uint256 k = 0x9000 + ids.length;
        vm.prank(vm.addr(keys[i]));
        try acc.delegate(ids[i], vm.addr(k), p, seed, address(usdc), fund) returns (bytes32 child) {
            ids.push(child);
            keys.push(k);
        } catch {}
    }

    function openChannel(uint256 seed, uint96 dep) external {
        uint256 i = _pick(seed);
        vm.prank(vm.addr(keys[i]));
        try ch.open(ids[i], payee, address(usdc), dep, 20, seed, "", 0) returns (bytes32 id) {
            channels.push(id);
            committedToPayee += dep;
        } catch {}
    }

    function settleChannel(uint256 seed, uint64 seq, uint96 bal, bool forge_) external {
        if (channels.length == 0) return;
        bytes32 id = channels[seed % channels.length];
        PaymentChannels.Channel memory c = ch.get(id);
        uint256 k = forge_ ? 0xBAD : _keyOf(c.payer);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(k, ch.updateDigest(id, c.payer, seq, bal));
        try ch.settle(id, seq, bal, abi.encodePacked(r, s, v)) {} catch {}
    }

    function closeChannel(uint256 seed, uint64 seq, uint96 bal) external {
        if (channels.length == 0) return;
        bytes32 id = channels[seed % channels.length];
        PaymentChannels.Channel memory c = ch.get(id);
        uint256 k = _keyOf(c.payer);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(k, ch.updateDigest(id, c.payer, seq, bal));
        vm.prank(vm.addr(k));
        try ch.beginClose(id, seq, bal, seed % 2 == 0 ? abi.encodePacked(r, s, v) : bytes("")) {} catch {}
    }

    function finalizeChannel(uint256 seed) external {
        if (channels.length == 0) return;
        bytes32 id = channels[seed % channels.length];
        PaymentChannels.Channel memory c = ch.get(id);
        vm.prank(vm.addr(_keyOf(c.payer)));
        try ch.finalizeClose(id) {} catch {}
    }

    function joinPool(uint256 seed, uint96 dep) external {
        uint256 i = _pick(seed);
        vm.prank(vm.addr(keys[i]));
        try pools.join(pid, ids[i], dep, "", 0) {
            committedToCoordinator += dep;
        } catch {}
    }

    function settlePool(uint256 seed, uint64 seq, uint96 bal, bool forge_) external {
        Pools.Update[] memory u = new Pools.Update[](ids.length);
        for (uint256 i = 0; i < ids.length; i++) {
            uint256 b = uint256(keccak256(abi.encode(seed, i))) % (uint256(bal) + 1);
            u[i] = _poolUpdate(forge_ && i == seed % ids.length ? 0xBAD : keys[i], ids[i], seq, b);
        }
        try pools.settle(pid, u) {} catch {}
    }

    function _poolUpdate(uint256 k, bytes32 id, uint64 seq, uint256 b) internal view returns (Pools.Update memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(k, pools.updateDigest(pid, id, seq, b));
        return Pools.Update(id, seq, b, abi.encodePacked(r, s, v));
    }

    function exitPool(uint256 seed, uint64 seq, uint96 bal) external {
        uint256 i = _pick(seed);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(keys[i], pools.updateDigest(pid, ids[i], seq, bal));
        vm.prank(vm.addr(keys[i]));
        try pools.beginExit(pid, ids[i], seq, bal, seed % 2 == 0 ? abi.encodePacked(r, s, v) : bytes("")) {} catch {}
    }

    function contestPool(uint256 seed, uint64 seq, uint96 bal) external {
        uint256 i = _pick(seed);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(keys[i], pools.updateDigest(pid, ids[i], seq, bal));
        try pools.contestExit(pid, Pools.Update(ids[i], seq, bal, abi.encodePacked(r, s, v))) {} catch {}
    }

    function finalizePool(uint256 seed) external {
        uint256 i = _pick(seed);
        vm.prank(vm.addr(keys[i]));
        try pools.finalizeExit(pid, ids[i]) {} catch {}
    }

    function warp(uint16 secs) external { vm.warp(block.timestamp + bound(secs, 0, 40)); }

    function _keyOf(bytes32 id) internal view returns (uint256) {
        for (uint256 i = 0; i < ids.length; i++) if (ids[i] == id) return keys[i];
        return 0xBAD;
    }
}

contract SystemInvariant is Test {
    AgentAccounts acc;
    PaymentChannels ch;
    Pools pools;
    MockERC20 usdc;
    SystemHandler h;
    bytes32 root;
    uint256 constant ROOT_KEY = 0x8001;
    uint128 constant ROOT_CAP = 50_000;

    function setUp() public {
        acc = new AgentAccounts();
        ch = new PaymentChannels(acc);
        pools = new Pools(acc);
        usdc = new MockERC20();
        address[] memory mods = new address[](2);
        mods[0] = address(ch);
        mods[1] = address(pools);
        acc.lockModules(mods);
        vm.warp(1_000_000);
        AgentAccounts.PolicyInput memory p;
        p.perTxMax = ROOT_CAP;
        p.perWindowMax = ROOT_CAP;
        p.windowSecs = 600;
        vm.prank(address(0xA0));
        root = acc.register(vm.addr(ROOT_KEY), p, 0);
        h = new SystemHandler(acc, ch, pools, usdc, root, ROOT_KEY);
        targetContract(address(h));
    }

    function invariant_token_conserved() public view {
        uint256 inAccounts;
        for (uint256 i = 0; i < h.count(); i++) inAccounts += acc.balanceOf(h.ids(i), address(usdc));
        assertEq(usdc.balanceOf(address(acc)), inAccounts, "accounts contract != sum of balances");
        uint256 total = usdc.balanceOf(address(acc)) + usdc.balanceOf(address(ch)) + usdc.balanceOf(address(pools))
            + usdc.balanceOf(h.payee()) + usdc.balanceOf(h.coordinator());
        assertEq(total, h.deposited(), "token created or lost");
    }

    function invariant_receivers_bounded_by_commitments() public view {
        assertLe(usdc.balanceOf(h.payee()), h.committedToPayee(), "payee got more than channel deposits");
        assertLe(usdc.balanceOf(h.coordinator()), h.committedToCoordinator(), "coordinator got more than pool deposits");
    }

    function invariant_root_window_bounds_commitments() public view {
        assertLe(acc.spentInWindow(root), ROOT_CAP, "root window over cap");
    }

    function invariant_closed_escrow_is_empty() public view {
        uint256 open;
        for (uint256 i = 0; i < h.channelCount(); i++) {
            PaymentChannels.Channel memory c = ch.get(h.channels(i));
            if (!c.closed) open += c.deposit - c.paid;
            assertLe(c.paid, c.deposit, "channel paid over deposit");
        }
        assertEq(usdc.balanceOf(address(ch)), open, "channel escrow != open channels' unspent");
        uint256 live;
        for (uint256 i = 0; i < h.count(); i++) {
            Pools.Claim memory c = pools.claimOf(h.pid(), h.ids(i));
            if (c.exists && !c.exited) live += c.deposit - c.paid;
            assertLe(c.paid, c.deposit, "claim paid over deposit");
        }
        assertEq(usdc.balanceOf(address(pools)), live, "pool escrow != live claims' unspent");
    }
}
