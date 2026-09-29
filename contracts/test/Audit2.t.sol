// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {PaymentChannels} from "../src/PaymentChannels.sol";
import {Pools} from "../src/Pools.sol";
import {MockERC20} from "./MockERC20.sol";

/// AUDIT-2 reproductions for PaymentChannels and Pools (see audits/AUDIT-2.md).
/// Tests marked `// FINDING A2-n` assert the DESIRED behaviour and fail against the current code.
/// Tests marked `// VERIFY A2-...` document what was attacked and found correct.

// ---------------------------------------------------------------------------- mocks

/// USDC-style token with a blacklist: transfers to or from a listed address revert.
contract BlacklistToken is MockERC20 {
    mapping(address => bool) public blacklisted;
    function setBlacklisted(address a, bool b) external { blacklisted[a] = b; }
    function _update(address from, address to, uint256 value) internal override {
        require(!blacklisted[from] && !blacklisted[to], "blacklisted");
        super._update(from, to, value);
    }
}

/// 1% fee-on-transfer token.
contract FeeToken is MockERC20 {
    function _update(address from, address to, uint256 value) internal override {
        if (from == address(0) || to == address(0)) return super._update(from, to, value);
        uint256 fee = value / 100;
        super._update(from, to, value - fee);
        super._update(from, address(0), fee);
    }
}

interface IReceiveHook { function onTokensReceived(address from, uint256 amount) external; }

/// ERC-777-style token: calls a hook on the recipient when it has one registered.
contract HookToken is MockERC20 {
    mapping(address => bool) public hooked;
    function setHook(address a, bool b) external { hooked[a] = b; }
    function _update(address from, address to, uint256 value) internal override {
        super._update(from, to, value);
        if (hooked[to]) IReceiveHook(to).onTokensReceived(from, value);
    }
}

/// A payee / coordinator / signer contract that reenters on receipt of tokens.
contract Reenterer is IReceiveHook {
    PaymentChannels ch;
    Pools pools;
    AgentAccounts acc;
    bytes32 public id;
    bytes32 public account;
    uint8 public mode; // 0 nothing, 1 ch.settle, 2 ch.finalizeClose, 3 pools.finalizeExit, 4 acc.refund
    uint64 seq; uint256 bal; bytes sig;
    bool public reentered;
    bytes public lastRevert;

    constructor(AgentAccounts a, PaymentChannels c, Pools p) { acc = a; ch = c; pools = p; }
    function arm(uint8 m, bytes32 id_, bytes32 account_, uint64 s, uint256 b, bytes calldata sg) external {
        mode = m; id = id_; account = account_; seq = s; bal = b; sig = sg;
    }
    function onTokensReceived(address, uint256) external {
        if (mode == 0) return;
        uint8 m = mode; mode = 0;
        bool ok; bytes memory ret;
        if (m == 1) (ok, ret) = address(ch).call(abi.encodeCall(ch.settle, (id, seq, bal, sig)));
        if (m == 2) (ok, ret) = address(ch).call(abi.encodeCall(ch.finalizeClose, (id)));
        if (m == 3) (ok, ret) = address(pools).call(abi.encodeCall(pools.finalizeExit, (id, account)));
        if (m == 4) (ok, ret) = address(acc).call(abi.encodeCall(acc.refund, (account, address(0), 1)));
        reentered = ok; lastRevert = ret;
    }
    // so a Reenterer can act as an account signer
    function call(address t, bytes calldata data) external returns (bytes memory) {
        (bool ok, bytes memory ret) = t.call(data);
        require(ok, string(ret));
        return ret;
    }
}

// ---------------------------------------------------------------------------- tests

contract Audit2Test is Test {
    function _epochOf(bytes32 pid_, bytes32 account_) internal view returns (uint64) {
        uint64 e = pools.claimOf(pid_, account_).epoch;
        return e == 0 ? 1 : e; // a not-yet-joined account will get epoch 1 on join
    }

    AgentAccounts acc;
    PaymentChannels ch;
    Pools pools;
    MockERC20 usdc;

    address owner = makeAddr("owner");
    address payee = makeAddr("payee");
    address coordinator = makeAddr("coordinator");
    uint256 signerKey = 0xA11CE;
    address signer;
    uint256 otherKey = 0xB0B;
    address other;
    bytes32 root;
    bytes32 pid;

    function setUp() public {
        acc = new AgentAccounts();
        ch = new PaymentChannels(acc);
        pools = new Pools(acc);
        usdc = new MockERC20();
        signer = vm.addr(signerKey);
        other = vm.addr(otherKey);
        address[] memory mods = new address[](2);
        mods[0] = address(ch);
        mods[1] = address(pools);
        acc.lockModules(mods);
        vm.warp(1_000_000);
        root = _account(signer, 0);
        _fund(root, usdc, 10_000);
        vm.prank(coordinator);
        pid = pools.create(address(usdc), 60, 0);
    }

    // ------------------------------------------------------------------ helpers

    function _account(address s, uint256 salt) internal returns (bytes32 id) {
        AgentAccounts.PolicyInput memory p;
        p.perTxMax = 5000;
        p.perWindowMax = 20_000;
        p.windowSecs = 3600;
        vm.prank(owner);
        id = acc.register(s, p, salt);
    }

    function _fund(bytes32 id, MockERC20 t, uint256 amount) internal {
        t.mint(address(this), amount);
        t.approve(address(acc), amount);
        acc.deposit(id, address(t), amount);
    }

    function _open(uint256 deposit, uint64 timeout, uint256 salt) internal returns (bytes32 id) {
        vm.prank(signer);
        id = ch.open(root, payee, address(usdc), deposit, timeout, salt, "", 0);
    }

    function _csig(uint256 key, bytes32 id, bytes32 account, uint64 seq, uint256 balance) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, ch.updateDigest(id, account, seq, balance));
        return abi.encodePacked(r, s, v);
    }

    function _psig(uint256 key, bytes32 pool, bytes32 account, uint64 seq, uint256 balance) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, pools.updateDigest(pool, account, _epochOf(pool, account), seq, balance));
        return abi.encodePacked(r, s, v);
    }

    function _pupd(uint256 key, bytes32 pool, bytes32 account, uint64 seq, uint256 balance) internal view returns (Pools.Update memory) {
        return Pools.Update(account, seq, balance, _psig(key, pool, account, seq, balance));
    }

    function _join(bytes32 pool, bytes32 account, address s, uint256 deposit) internal {
        vm.prank(s);
        pools.join(pool, account, deposit, "", 0);
    }

    // =========================================================================== FINDINGS

    // FINDING A2-1 (High): ordering by payer-chosen `seq` lets the payer renege on unsettled updates.
    // The payer signs (seq = huge, balance = already paid) and applies it in beginClose; the payee's
    // genuine (seq 7, balance 450) update is now "stale" and can never be settled. Whitepaper 6.4
    // says the payee may answer a close with "a higher one" - higher balance, which it holds.
    function test_A2_1_payer_seq_jump_invalidates_payees_unsettled_update() public {
        bytes32 id = _open(1000, 60, 0);
        // honest history: payee holds seq 7 / balance 450, not yet settled
        bytes memory payeeUpdate = _csig(signerKey, id, root, 7, 450);
        // payer jumps the seq with no extra payment and starts closing on it
        bytes memory jump = _csig(signerKey, id, root, type(uint64).max, 0);
        vm.prank(signer);
        ch.beginClose(id, type(uint64).max, 0, jump);
        // desired: the payee can still collect the 450 it was paid off-chain
        ch.settle(id, 7, 450, payeeUpdate);
        assertEq(usdc.balanceOf(payee), 450, "payee should be able to settle its higher-balance update");
    }

    // FINDING A2-1 (High), pool variant: a member exits on a seq-jumped zero update; the coordinator's
    // genuine (seq 5, balance 400) update cannot contest it and the member walks away with everything.
    function test_A2_1b_pool_member_seq_jump_defeats_contest() public {
        _join(pid, root, signer, 500);
        Pools.Update memory coordHolds = _pupd(signerKey, pid, root, 5, 400);
        bytes memory jump = _psig(signerKey, pid, root, type(uint64).max, 0);
        vm.prank(signer);
        pools.beginExit(pid, root, type(uint64).max, 0, jump);
        vm.warp(block.timestamp + 30);
        // desired: the contest with the higher balance succeeds
        pools.contestExit(pid, coordHolds);
        assertEq(usdc.balanceOf(coordinator), 400, "coordinator should be able to contest with the higher balance");
    }

    // FINDING A2-2 (High): after exit and rejoin the claim's seq resets to 0 but the typed data does
    // not change, so any update signed during an EARLIER membership (same pool, same account) is a
    // valid update against the NEW claim as long as its balance fits the new deposit. The coordinator
    // is paid twice for the same signed value.
    function test_A2_2_rejoin_replays_old_pool_update() public {
        _join(pid, root, signer, 500);
        Pools.Update memory old = _pupd(signerKey, pid, root, 5, 400);
        // membership 1: the update is settled honestly
        Pools.Update[] memory b = new Pools.Update[](1);
        b[0] = old;
        pools.settle(pid, b);
        assertEq(usdc.balanceOf(coordinator), 400);
        vm.prank(signer);
        pools.beginExit(pid, root, 0, 0, "");
        vm.warp(block.timestamp + 60);
        vm.prank(signer);
        pools.finalizeExit(pid, root);
        assertEq(acc.balanceOf(root, address(usdc)), 9600);
        // membership 2: fresh claim, nothing signed yet
        _join(pid, root, signer, 500);
        // anyone replays the membership-1 update against the fresh claim
        vm.prank(makeAddr("anyone"));
        try pools.settle(pid, b) {} catch {}
        // desired: the fresh claim is untouched
        assertEq(pools.claimOf(pid, root).paid, 0, "old membership's update replayed against new claim");
        assertEq(usdc.balanceOf(coordinator), 400, "coordinator paid twice for one signed balance");
    }

    // FINDING A2-3 (Medium): the coordinator chooses timeoutSecs unbounded; `uint64(block.timestamp) +
    // timeoutSecs` is checked arithmetic, so a timeout near 2^64 makes every beginExit revert and the
    // members' deposits are locked for ever (the coordinator can still collect what they sign).
    function test_A2_3_pool_timeout_overflow_locks_member_deposits() public {
        vm.prank(coordinator);
        bytes32 trap;
        try pools.create(address(usdc), type(uint64).max, 1) returns (bytes32 p) { trap = p; }
        catch { return; } // desired: an absurd timeout is refused at create - pass
        _join(trap, root, signer, 500);
        // desired: the member can still start an exit
        vm.prank(signer);
        pools.beginExit(trap, root, 0, 0, "");
    }

    // FINDING A2-4 (Medium): rotating the signer (owner or any ancestor signer) invalidates every
    // unsettled update the payee holds, so the payer side can renege by administration alone.
    // Reference-faithful ("current signer") but it contradicts whitepaper 6.4's settlement guarantee.
    function test_A2_4_signer_rotation_lets_payer_renege_on_unsettled_updates() public {
        bytes32 id = _open(1000, 60, 0);
        bytes memory payeeUpdate = _csig(signerKey, id, root, 3, 700);
        vm.prank(owner);
        acc.rotateSigner(root, other);
        vm.prank(other);
        ch.beginClose(id, 0, 0, "");
        // desired: the update signed by the signer that was current when it was signed still settles
        ch.settle(id, 3, 700, payeeUpdate);
        assertEq(usdc.balanceOf(payee), 700, "payee lost 700 to a signer rotation");
    }

    // FINDING A2-5 (Low): commit refuses AgentAccounts and address(0) as payee but not the modules
    // themselves; a channel whose payee is the channel or pool contract pays into unassigned escrow.
    function test_A2_5_payee_may_be_a_module_and_strands_tokens() public {
        vm.prank(signer);
        try ch.open(root, address(ch), address(usdc), 300, 60, 7, "", 0) returns (bytes32 id) {
            ch.settle(id, 1, 300, _csig(signerKey, id, root, 1, 300));
            // 300 now sits in the channel contract belonging to no channel
            assertEq(usdc.balanceOf(address(ch)), 0, "tokens stranded in the channel contract");
        } catch {
            // desired: refused
        }
    }

    // =========================================================================== VERIFY

    // VERIFY A2-sig-domain: a channel update cannot be replayed on another channel, on a pool, on the
    // same channel under another chain id, and a pool update cannot be replayed on another pool or a
    // channel.
    function test_verify_signature_domains() public {
        bytes32 id0 = _open(1000, 60, 0);
        bytes32 id1 = _open(1000, 60, 1);
        bytes memory s = _csig(signerKey, id0, root, 1, 100);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id1, 1, 100, s);
        // pool with the same seq/balance
        _join(pid, root, signer, 1000);
        Pools.Update[] memory b = new Pools.Update[](1);
        b[0] = Pools.Update(root, 1, 100, s);
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.settle(pid, b);
        // pool update on a channel and on another pool
        bytes memory ps = _psig(signerKey, pid, root, 1, 100);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id0, 1, 100, ps);
        vm.prank(coordinator);
        bytes32 pid2 = pools.create(address(usdc), 60, 2);
        _join(pid2, root, signer, 1000);
        b[0] = Pools.Update(root, 1, 100, ps);
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.settle(pid2, b);
        // chain id is in the domain
        vm.chainId(999);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id0, 1, 100, s);
        vm.chainId(31337);
        ch.settle(id0, 1, 100, s);
        assertEq(usdc.balanceOf(payee), 100);
    }

    // VERIFY A2-typehash: the digest matches an independent EIP-712 encoding of the declared type strings.
    function test_verify_eip712_encoding() public view {
        bytes32 domain = keccak256(abi.encode(
            keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
            keccak256("FoliantPaymentChannels"), keccak256("1"), block.chainid, address(ch)));
        bytes32 structHash = keccak256(abi.encode(
            keccak256("ChannelUpdate(bytes32 channel,bytes32 account,uint64 seq,uint256 balance)"),
            bytes32(uint256(1)), bytes32(uint256(2)), uint64(3), uint256(4)));
        assertEq(ch.updateDigest(bytes32(uint256(1)), bytes32(uint256(2)), 3, 4),
            keccak256(abi.encodePacked("\x19\x01", domain, structHash)));
        bytes32 pdomain = keccak256(abi.encode(
            keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
            keccak256("FoliantPools"), keccak256("1"), block.chainid, address(pools)));
        bytes32 pstruct = keccak256(abi.encode(
            keccak256("PoolUpdate(bytes32 pool,bytes32 account,uint64 epoch,uint64 seq,uint256 balance)"),
            bytes32(uint256(1)), bytes32(uint256(2)), uint64(7), uint64(3), uint256(4)));
        assertEq(pools.updateDigest(bytes32(uint256(1)), bytes32(uint256(2)), 7, 3, 4),
            keccak256(abi.encodePacked("\x19\x01", pdomain, pstruct)));
    }

    // VERIFY A2-malleability / zero-length / garbage signatures: settle rejects a high-s copy of a valid
    // signature, an empty signature and a 64-byte one; beginClose with an empty sig skips verification
    // but a garbage sig reverts rather than being ignored.
    function test_verify_signature_shapes() public {
        bytes32 id = _open(1000, 60, 0);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerKey, ch.updateDigest(id, root, 1, 100));
        bytes32 n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;
        bytes32 s2 = bytes32(uint256(n) - uint256(s));
        uint8 v2 = v == 27 ? 28 : 27;
        bytes memory mall = abi.encodePacked(r, s2, v2);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id, 1, 100, mall);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id, 1, 100, "");
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id, 1, 100, abi.encodePacked(r, s));
        vm.prank(signer);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.beginClose(id, 1, 100, hex"deadbeef");
        Pools.Update[] memory b = new Pools.Update[](1);
        _join(pid, root, signer, 100);
        b[0] = Pools.Update(root, 1, 50, "");
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.settle(pid, b);
        vm.prank(signer);
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.beginExit(pid, root, 1, 50, hex"00");
    }

    // VERIFY A2-id-reuse: a closed channel keeps its id; the same salt can never be reopened, so an
    // old update cannot land on a "new" channel with the same id.
    function test_verify_closed_channel_id_not_reusable() public {
        bytes32 id = _open(1000, 60, 0);
        vm.prank(signer);
        ch.beginClose(id, 0, 0, "");
        vm.warp(block.timestamp + 60);
        vm.prank(signer);
        ch.finalizeClose(id);
        vm.prank(signer);
        vm.expectRevert(PaymentChannels.ChannelExists.selector);
        ch.open(root, payee, address(usdc), 1000, 60, 0, "", 0);
    }

    // VERIFY A2-open-auth: only the account's current signer can open / join for it; the module passes
    // msg.sender and AgentAccounts.commit checks it (A1-7 condition).
    function test_verify_open_and_join_authenticate_signer() public {
        vm.prank(other);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        ch.open(root, payee, address(usdc), 100, 60, 0, "", 0);
        vm.prank(owner);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        ch.open(root, payee, address(usdc), 100, 60, 0, "", 0);
        vm.prank(other);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        pools.join(pid, root, 100, "", 0);
        // non-module cannot commit directly
        vm.prank(signer);
        vm.expectRevert(AgentAccounts.Unauthorized.selector);
        acc.commit(root, signer, address(usdc), payee, 100, "", 0);
    }

    // VERIFY A2-reverting-payee: a payee that reverts on receipt (USDC blacklist) cannot trap the
    // payer: beginClose with no update and finalizeClose still return the whole deposit.
    function test_verify_reverting_payee_does_not_block_close() public {
        BlacklistToken bt = new BlacklistToken();
        _fund(root, bt, 1000);
        vm.prank(signer);
        bytes32 id = ch.open(root, payee, address(bt), 1000, 60, 0, "", 0);
        bt.setBlacklisted(payee, true);
        bytes memory u = _csig(signerKey, id, root, 1, 400);
        // payee cannot settle, payer cannot apply the update either
        vm.expectRevert("blacklisted");
        ch.settle(id, 1, 400, u);
        vm.prank(signer);
        vm.expectRevert("blacklisted");
        ch.beginClose(id, 1, 400, u);
        // but the payer closes without it
        vm.prank(signer);
        ch.beginClose(id, 0, 0, "");
        vm.warp(block.timestamp + 60);
        vm.prank(signer);
        ch.finalizeClose(id);
        assertEq(acc.balanceOf(root, address(bt)), 1000);
        assertEq(bt.balanceOf(address(ch)), 0);
    }

    // VERIFY A2-reverting-coordinator: a coordinator that reverts on receipt blocks settle (its own
    // loss) but not a member's exit: beginExit with no update, finalizeExit refunds everything.
    function test_verify_reverting_coordinator_does_not_block_exit() public {
        BlacklistToken bt = new BlacklistToken();
        _fund(root, bt, 1000);
        vm.prank(coordinator);
        bytes32 p = pools.create(address(bt), 60, 5);
        _join(p, root, signer, 1000);
        bt.setBlacklisted(coordinator, true);
        Pools.Update[] memory b = new Pools.Update[](1);
        b[0] = _pupd(signerKey, p, root, 1, 400);
        vm.expectRevert("blacklisted");
        pools.settle(p, b);
        vm.prank(signer);
        vm.expectRevert("blacklisted");
        pools.beginExit(p, root, 1, 400, b[0].sig);
        vm.prank(signer);
        pools.beginExit(p, root, 0, 0, "");
        vm.warp(block.timestamp + 30);
        vm.expectRevert("blacklisted");
        pools.contestExit(p, b[0]);
        vm.warp(block.timestamp + 30);
        vm.prank(signer);
        pools.finalizeExit(p, root);
        assertEq(acc.balanceOf(root, address(bt)), 1000);
        assertEq(bt.balanceOf(address(pools)), 0);
    }

    // VERIFY A2-reentrancy: a payee that reenters settle / finalizeClose from the token's receive hook
    // is blocked by the guard; a signer contract reentering finalizeClose during the refund pull is
    // blocked; a module-impersonating refund from the hook is refused by AgentAccounts.
    function test_verify_reentrancy_on_payout_and_refund() public {
        HookToken ht = new HookToken();
        Reenterer evil = new Reenterer(acc, ch, pools);
        ht.setHook(address(evil), true);
        _fund(root, ht, 1000);
        vm.prank(signer);
        bytes32 id = ch.open(root, address(evil), address(ht), 1000, 60, 0, "", 0);
        // hook tries to settle a second, higher update while the first payout is in flight
        bytes memory u1 = _csig(signerKey, id, root, 1, 100);
        bytes memory u2 = _csig(signerKey, id, root, 2, 300);
        evil.arm(1, id, root, 2, 300, u2);
        ch.settle(id, 1, 100, u1);
        assertFalse(evil.reentered(), "reentered settle");
        assertEq(ht.balanceOf(address(evil)), 100);
        assertEq(ch.get(id).paid, 100);
        // hook tries to refund to an account through AgentAccounts (not a module)
        evil.arm(4, id, root, 0, 0, "");
        ch.settle(id, 2, 300, u2);
        assertFalse(evil.reentered(), "non-module refund accepted");
        assertEq(ht.balanceOf(address(evil)), 300);
        // AgentAccounts side: the refund pull goes to `acc`, which has no hook, so nothing to reenter;
        // and a second finalizeClose is refused by `closed`.
        vm.prank(signer);
        ch.beginClose(id, 0, 0, "");
        vm.warp(block.timestamp + 60);
        vm.prank(signer);
        ch.finalizeClose(id);
        assertEq(acc.balanceOf(root, address(ht)), 700);
        vm.prank(signer);
        vm.expectRevert(PaymentChannels.ChannelClosed.selector);
        ch.finalizeClose(id);
        assertEq(ht.allowance(address(ch), address(acc)), 0, "leftover approval");
    }

    // VERIFY A2-fee-token: deposit is recorded as received; balances are bounded by the received
    // amount; the refund of the remainder does not underflow and credits what actually arrives.
    function test_verify_fee_on_transfer_accounting() public {
        FeeToken ft = new FeeToken();
        _fund(root, ft, 10_000);                       // account credited 9_900
        vm.prank(signer);
        bytes32 id = ch.open(root, payee, address(ft), 1000, 60, 0, "", 0);
        PaymentChannels.Channel memory c = ch.get(id);
        assertEq(c.deposit, 990);
        assertEq(acc.spentInWindow(root), 1000);      // policy saw the requested amount
        bytes memory over = _csig(signerKey, id, root, 1, 1000);
        vm.expectRevert(abi.encodeWithSelector(PaymentChannels.BadUpdate.selector, "balance exceeds deposit"));
        ch.settle(id, 1, 1000, over);
        ch.settle(id, 1, 500, _csig(signerKey, id, root, 1, 500));
        assertEq(ft.balanceOf(payee), 495);
        vm.prank(signer);
        ch.beginClose(id, 0, 0, "");
        vm.warp(block.timestamp + 60);
        vm.prank(signer);
        ch.finalizeClose(id);
        assertEq(ft.balanceOf(address(ch)), 0);
        assertEq(acc.balanceOf(root, address(ft)), 9900 - 1000 + 486); // 490 pulled, fee 4 burnt
        // pool side: same shape
        vm.prank(coordinator);
        bytes32 p = pools.create(address(ft), 60, 9);
        _join(p, root, signer, 1000);
        assertEq(pools.claimOf(p, root).deposit, 990);
        bytes memory full = _psig(signerKey, p, root, 1, 990);
        vm.prank(signer);
        pools.beginExit(p, root, 1, 990, full);
        vm.warp(block.timestamp + 60);
        vm.prank(signer);
        pools.finalizeExit(p, root); // remaining 0: no refund call, no revert
        assertTrue(pools.claimOf(p, root).exited);
    }

    // VERIFY A2-settle-griefing: anyone may settle, but only the payer's valid, higher-seq,
    // non-decreasing updates apply; a third party cannot decrease `paid` or apply a stale one.
    // Also: settle is still allowed after the close window has elapsed (until finalizeClose), as in
    // the reference's payee_settle_channel; the payee's higher update front-runs the finalize.
    function test_verify_settle_after_window_until_finalize() public {
        bytes32 id = _open(1000, 60, 0);
        bytes memory u1 = _csig(signerKey, id, root, 1, 100);
        vm.prank(signer);
        ch.beginClose(id, 1, 100, u1);
        vm.warp(block.timestamp + 61);
        ch.settle(id, 2, 600, _csig(signerKey, id, root, 2, 600));
        vm.prank(signer);
        ch.finalizeClose(id);
        assertEq(usdc.balanceOf(payee), 600);
        assertEq(acc.balanceOf(root, address(usdc)), 9400);
    }

    // VERIFY A2-pool-batch: duplicate accounts in one batch apply once; a non-member entry is
    // skipped before verification; the caller need not be the coordinator; total is one transfer.
    function test_verify_pool_batch_shapes() public {
        _join(pid, root, signer, 1000);
        bytes32 nonMember = _account(other, 1);
        Pools.Update[] memory b = new Pools.Update[](4);
        b[0] = _pupd(signerKey, pid, root, 1, 100);
        b[1] = _pupd(signerKey, pid, root, 1, 100);          // duplicate: skipped (stale)
        b[2] = Pools.Update(nonMember, 1, 100, hex"00");      // not a member: skipped before verify
        b[3] = _pupd(signerKey, pid, root, 2, 250);          // applied
        vm.prank(makeAddr("anyone"));
        uint256 total = pools.settle(pid, b);
        assertEq(total, 250);
        assertEq(usdc.balanceOf(coordinator), 250);
        assertEq(pools.claimOf(pid, root).seq, 2);
    }

    // VERIFY A2-contest-vs-settle: after the exit window has elapsed contestExit is refused but settle
    // still applies a higher update until finalizeExit runs (reference-identical; noted as A2-7).
    function test_verify_settle_after_exit_window() public {
        _join(pid, root, signer, 1000);
        vm.prank(signer);
        pools.beginExit(pid, root, 0, 0, "");
        vm.warp(block.timestamp + 60);
        Pools.Update[] memory b = new Pools.Update[](1);
        b[0] = _pupd(signerKey, pid, root, 1, 300);
        vm.expectRevert(Pools.NoExitWindow.selector);
        pools.contestExit(pid, b[0]);
        assertEq(pools.settle(pid, b), 300);
        vm.prank(signer);
        pools.finalizeExit(pid, root);
        assertEq(acc.balanceOf(root, address(usdc)), 9700);
        // after exit nothing applies
        b[0] = _pupd(signerKey, pid, root, 2, 400);
        assertEq(pools.settle(pid, b), 0);
    }

    // VERIFY A2-timeouts: timeout 0 refused in both; channel timeout overflow only hurts the payer
    // who chose it (payee can still settle).
    function test_verify_timeout_bounds() public {
        vm.prank(signer);
        vm.expectRevert(abi.encodeWithSelector(PaymentChannels.BadUpdate.selector, "timeout out of range"));
        ch.open(root, payee, address(usdc), 100, 0, 0, "", 0);
        vm.prank(coordinator);
        vm.expectRevert(abi.encodeWithSelector(Pools.BadUpdate.selector, "timeout out of range"));
        pools.create(address(usdc), 0, 3);
        // FIXED A2-3: timeouts above MAX_TIMEOUT are refused at open/create, so the exit arithmetic cannot overflow
        vm.prank(signer);
        vm.expectRevert(abi.encodeWithSelector(PaymentChannels.BadUpdate.selector, "timeout out of range"));
        ch.open(root, payee, address(usdc), 100, type(uint64).max, 1, "", 0);
        vm.prank(coordinator);
        vm.expectRevert(abi.encodeWithSelector(Pools.BadUpdate.selector, "timeout out of range"));
        pools.create(address(usdc), 30 days + 1, 4);
        bytes32 id = _open(1000, 30 days, 0);
        vm.prank(signer);
        ch.beginClose(id, 0, 0, "");
    }

    // VERIFY A2-refund-accounting: a refund is not a spend (window unchanged), credits the exact
    // remainder, and the module's approval is fully consumed.
    function test_verify_refund_not_a_spend_and_no_leftover_approval() public {
        bytes32 id = _open(1000, 60, 0);
        ch.settle(id, 1, 250, _csig(signerKey, id, root, 1, 250));
        vm.prank(signer);
        ch.beginClose(id, 0, 0, "");
        vm.warp(block.timestamp + 60);
        vm.prank(signer);
        ch.finalizeClose(id);
        assertEq(acc.spentInWindow(root), 1000);
        assertEq(acc.balanceOf(root, address(usdc)), 9750);
        assertEq(usdc.allowance(address(ch), address(acc)), 0);
        assertEq(usdc.balanceOf(address(ch)), 0);
    }

    // VERIFY A2-contract-signer: a contract signer can open, close and finalize (msg.sender), but can
    // never produce an update (no ERC-1271), so a smart-account payer cannot pay through a channel.
    function test_verify_contract_signer_cannot_sign_updates() public {
        Reenterer bot = new Reenterer(acc, ch, pools);
        bytes32 a = _account(address(bot), 2);
        _fund(a, usdc, 1000);
        bytes memory ret = bot.call(address(ch), abi.encodeCall(ch.open, (a, payee, address(usdc), 500, 60, 0, "", 0)));
        bytes32 id = abi.decode(ret, (bytes32));
        assertEq(ch.get(id).deposit, 500);
        // no signature the bot can make is accepted
        bytes memory forged = _csig(otherKey, id, a, 1, 100);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id, 1, 100, forged);
        bot.call(address(ch), abi.encodeCall(ch.beginClose, (id, 0, 0, "")));
        vm.warp(block.timestamp + 60);
        bot.call(address(ch), abi.encodeCall(ch.finalizeClose, (id)));
        assertEq(acc.balanceOf(a, address(usdc)), 1000);
    }

    // VERIFY A2-lost-key: if the signer key is lost the owner rotates and the new signer closes.
    function test_verify_owner_rotation_recovers_stuck_channel() public {
        bytes32 id = _open(1000, 60, 0);
        vm.prank(owner);
        acc.rotateSigner(root, other);
        vm.prank(other);
        ch.beginClose(id, 0, 0, "");
        vm.warp(block.timestamp + 60);
        vm.prank(other);
        ch.finalizeClose(id);
        assertEq(acc.balanceOf(root, address(usdc)), 10_000);
    }

    // =========================================================================== VERIFY-2 (commit d15bce8)

    function _psigE(uint256 key, bytes32 pool, bytes32 account, uint64 epoch, uint64 seq, uint256 balance) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, pools.updateDigest(pool, account, epoch, seq, balance));
        return abi.encodePacked(r, s, v);
    }

    // VERIFY-2 balance ordering (channels): the same balance cannot be applied twice, a lower balance
    // with a higher seq is stale, a seq that goes backwards with a higher balance applies (seq is
    // bookkeeping only), beginClose with a stale signed update still sets closingAt, settling exactly
    // the deposit then closing takes the remaining == 0 path without touching AgentAccounts.
    function test_V2_channel_balance_ordering() public {
        bytes32 id = _open(1000, 60, 0);
        bytes memory u5_300 = _csig(signerKey, id, root, 5, 300);
        bytes memory u9_300 = _csig(signerKey, id, root, 9, 300);
        bytes memory u9_200 = _csig(signerKey, id, root, 9, 200);
        bytes memory u2_1000 = _csig(signerKey, id, root, 2, 1000);
        ch.settle(id, 5, 300, u5_300);
        vm.expectRevert(PaymentChannels.StaleUpdate.selector);
        ch.settle(id, 5, 300, u5_300);              // same balance twice
        vm.expectRevert(PaymentChannels.StaleUpdate.selector);
        ch.settle(id, 9, 300, u9_300);              // same balance, higher seq
        vm.expectRevert(PaymentChannels.StaleUpdate.selector);
        ch.settle(id, 9, 200, u9_200);              // lower balance, higher seq
        // payer's seq jump at the settled balance no longer blocks anything and still opens the window
        bytes memory jump = _csig(signerKey, id, root, type(uint64).max, 300);
        vm.prank(signer);
        ch.beginClose(id, type(uint64).max, 300, jump);
        assertEq(ch.get(id).closingAt, uint64(block.timestamp) + 60, "closingAt not set on stale signed close");
        assertEq(ch.get(id).paid, 300);
        // a lower seq with the full balance applies: remaining becomes 0
        ch.settle(id, 2, 1000, u2_1000);
        assertEq(usdc.balanceOf(payee), 1000);
        vm.warp(block.timestamp + 60);
        uint256 accBefore = usdc.balanceOf(address(acc));
        vm.prank(signer);
        vm.expectEmit(true, false, false, true);
        emit PaymentChannels.Closed(id, 0);
        ch.finalizeClose(id);
        assertEq(usdc.balanceOf(address(acc)), accBefore);
        assertEq(usdc.balanceOf(address(ch)), 0);
        assertTrue(ch.get(id).closed);
    }

    // VERIFY-2 balance ordering (pools): one signed balance pays the coordinator once across any number
    // of batches, beginExit and contestExit; equal or lower balances are skipped / StaleUpdate; the
    // out-of-range check is still reached on the gated path.
    function test_V2_pool_balance_ordering_no_double_pay() public {
        _join(pid, root, signer, 500);
        Pools.Update memory a = _pupd(signerKey, pid, root, 1, 200);
        Pools.Update memory b = _pupd(signerKey, pid, root, 7, 200);   // same balance, other seq
        Pools.Update memory c = _pupd(signerKey, pid, root, 8, 150);   // lower balance, higher seq
        Pools.Update memory d = _pupd(signerKey, pid, root, 3, 501);   // over deposit
        Pools.Update[] memory batch = new Pools.Update[](3);
        batch[0] = a; batch[1] = b; batch[2] = c;
        assertEq(pools.settle(pid, batch), 200);
        assertEq(pools.settle(pid, batch), 0);                          // second batch: all stale
        assertEq(usdc.balanceOf(coordinator), 200);
        batch = new Pools.Update[](1);
        batch[0] = d;
        vm.expectRevert(abi.encodeWithSelector(Pools.BadUpdate.selector, "balance out of range"));
        pools.settle(pid, batch);
        // beginExit with the already-settled balance: no payout, window opens
        vm.prank(signer);
        pools.beginExit(pid, root, 7, 200, b.sig);
        assertEq(usdc.balanceOf(coordinator), 200);
        assertTrue(pools.claimOf(pid, root).exitAt != 0);
        vm.expectRevert(Pools.StaleUpdate.selector);
        pools.contestExit(pid, a);
        vm.expectRevert(Pools.StaleUpdate.selector);
        pools.contestExit(pid, c);
        Pools.Update memory e = _pupd(signerKey, pid, root, 2, 350);   // lower seq, higher balance: applies
        pools.contestExit(pid, e);
        assertEq(usdc.balanceOf(coordinator), 350);
        assertEq(pools.claimOf(pid, root).seq, 2);
    }

    // VERIFY-2 epoch: first join is epoch 1 (default struct 0 + 1); every rejoin increments; an
    // epoch-N signature is Unauthorized on epoch N+1 whatever its balance; between finalizeExit and
    // rejoin nothing applies (settle skips, beginExit/contestExit NotMember); epoch is never reset.
    function test_V2_epoch_lifecycle() public {
        assertEq(pools.claimOf(pid, root).epoch, 0);
        _join(pid, root, signer, 500);
        assertEq(pools.claimOf(pid, root).epoch, 1);
        bytes memory e1 = _psigE(signerKey, pid, root, 1, 5, 400);
        bytes memory e0 = _psigE(signerKey, pid, root, 0, 5, 400);
        Pools.Update[] memory b = new Pools.Update[](1);
        b[0] = Pools.Update(root, 5, 400, e0);
        vm.expectRevert(Pools.Unauthorized.selector);           // epoch 0 never valid
        pools.settle(pid, b);
        b[0] = Pools.Update(root, 5, 400, e1);
        assertEq(pools.settle(pid, b), 400);
        vm.prank(signer);
        pools.beginExit(pid, root, 0, 0, "");
        vm.warp(block.timestamp + 60);
        vm.prank(signer);
        pools.finalizeExit(pid, root);
        // exited, not yet rejoined: the epoch-1 update is dead on every path
        assertEq(pools.settle(pid, b), 0);
        vm.expectRevert(Pools.NotMember.selector);
        pools.contestExit(pid, b[0]);
        vm.prank(signer);
        vm.expectRevert(Pools.NotMember.selector);
        pools.beginExit(pid, root, 5, 400, e1);
        assertEq(pools.claimOf(pid, root).epoch, 1, "epoch reset on exit");
        // rejoin with a deposit that would have fitted the old balance
        _join(pid, root, signer, 500);
        assertEq(pools.claimOf(pid, root).epoch, 2);
        assertEq(pools.claimOf(pid, root).paid, 0);
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.settle(pid, b);                                    // FIXED A2-2
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.beginExit(pid, root, 5, 400, e1);
        vm.prank(signer);
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.beginExit(pid, root, 5, 400, e1);
        // an epoch-2 update from the same key does apply, once
        b[0] = Pools.Update(root, 1, 100, _psigE(signerKey, pid, root, 2, 1, 100));
        assertEq(pools.settle(pid, b), 100);
        assertEq(pools.settle(pid, b), 0);
        // and a third membership is epoch 3
        vm.prank(signer);
        pools.beginExit(pid, root, 0, 0, "");
        vm.warp(block.timestamp + 60);
        vm.prank(signer);
        pools.finalizeExit(pid, root);
        _join(pid, root, signer, 100);
        assertEq(pools.claimOf(pid, root).epoch, 3);
        assertEq(usdc.balanceOf(coordinator), 500);
    }

    // VERIFY-2 epoch is bound to the (pool, account) pair: the same account's epoch-1 update on
    // pool A is not valid on pool B where it is also at epoch 1.
    function test_V2_epoch_is_per_pool() public {
        vm.prank(coordinator);
        bytes32 pid2 = pools.create(address(usdc), 60, 2);
        _join(pid, root, signer, 500);
        _join(pid2, root, signer, 500);
        Pools.Update[] memory b = new Pools.Update[](1);
        b[0] = Pools.Update(root, 1, 100, _psigE(signerKey, pid, root, 1, 1, 100));
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.settle(pid2, b);
        assertEq(pools.settle(pid, b), 100);
    }

    // VERIFY-2 snapshotted signers: after a rotation, the opening signer's updates still settle (FIXED
    // A2-4) and the new signer's do not; close/exit authority is the CURRENT signer, so the new signer
    // closes with an empty update (and must not pass its own signed one); the pool claim behaves the
    // same; a rejoin after rotation snapshots the new signer.
    function test_V2_snapshot_signers_vs_current_authority() public {
        bytes32 id = _open(1000, 60, 0);
        _join(pid, root, signer, 500);
        bytes memory oldU = _csig(signerKey, id, root, 1, 400);
        Pools.Update memory oldP = _pupd(signerKey, pid, root, 1, 200);
        vm.prank(owner);
        acc.rotateSigner(root, other);
        assertEq(ch.get(id).signer, signer);
        assertEq(pools.claimOf(pid, root).signer, signer);
        // updates: old signer valid, new signer invalid
        bytes memory newU = _csig(otherKey, id, root, 2, 500);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.settle(id, 2, 500, newU);
        ch.settle(id, 1, 400, oldU);
        Pools.Update memory newP = _pupd(otherKey, pid, root, 2, 300);
        Pools.Update[] memory b = new Pools.Update[](1);
        b[0] = newP;
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.settle(pid, b);
        b[0] = oldP;
        assertEq(pools.settle(pid, b), 200);
        // authority: old signer cannot close/exit, new signer can, but only with an empty update
        vm.prank(signer);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.beginClose(id, 0, 0, "");
        vm.prank(other);
        vm.expectRevert(PaymentChannels.Unauthorized.selector);
        ch.beginClose(id, 2, 500, newU);
        vm.prank(other);
        ch.beginClose(id, 0, 0, "");
        vm.prank(signer);
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.beginExit(pid, root, 0, 0, "");
        vm.prank(other);
        pools.beginExit(pid, root, 0, 0, "");
        // the rotated-away key can still move the committed remainder to the payee/coordinator during
        // the window: that value was authorised at open/join, so this is the documented design
        bytes memory drain = _csig(signerKey, id, root, 3, 1000);
        ch.settle(id, 3, 1000, drain);
        Pools.Update memory drainP = _pupd(signerKey, pid, root, 3, 500);
        pools.contestExit(pid, drainP);
        vm.warp(block.timestamp + 60);
        vm.prank(other);
        ch.finalizeClose(id);
        vm.prank(other);
        pools.finalizeExit(pid, root);
        assertEq(usdc.balanceOf(payee), 1000);
        assertEq(usdc.balanceOf(coordinator), 500);
        assertEq(acc.balanceOf(root, address(usdc)), 8500);
        // rejoin after the rotation: the claim now snapshots the new signer at epoch 2
        vm.prank(other);
        pools.join(pid, root, 100, "", 0);
        Pools.Claim memory c = pools.claimOf(pid, root);
        assertEq(c.signer, other);
        assertEq(c.epoch, 2);
        b[0] = Pools.Update(root, 1, 50, _psigE(signerKey, pid, root, 2, 1, 50)); // old key, right epoch
        vm.expectRevert(Pools.Unauthorized.selector);
        pools.settle(pid, b);
        b[0] = Pools.Update(root, 1, 50, _psigE(otherKey, pid, root, 2, 1, 50));
        assertEq(pools.settle(pid, b), 50);
    }

    // VERIFY-2 MAX_TIMEOUT boundary and the arithmetic it protects: 30 days accepted, 30 days + 1
    // refused, and closingAt/exitAt are computed without overflow at the bound.
    function test_V2_max_timeout_boundary() public {
        assertEq(ch.MAX_TIMEOUT(), 30 days);
        assertEq(pools.MAX_TIMEOUT(), 30 days);
        vm.prank(signer);
        vm.expectRevert(abi.encodeWithSelector(PaymentChannels.BadUpdate.selector, "timeout out of range"));
        ch.open(root, payee, address(usdc), 100, 30 days + 1, 3, "", 0);
        bytes32 id = _open(100, 30 days, 4);
        vm.prank(signer);
        ch.beginClose(id, 0, 0, "");
        assertEq(ch.get(id).closingAt, uint64(block.timestamp) + 30 days);
        vm.prank(coordinator);
        bytes32 p = pools.create(address(usdc), 30 days, 6);
        _join(p, root, signer, 100);
        vm.prank(signer);
        pools.beginExit(p, root, 0, 0, "");
        assertEq(pools.claimOf(p, root).exitAt, uint64(block.timestamp) + 30 days);
    }

    // VERIFY-2 module addresses as payee: refused at open, at transfer and at commit (FIXED A2-5); an
    // ordinary contract payee is still fine.
    function test_V2_module_payee_refused() public {
        vm.prank(signer);
        vm.expectRevert(AgentAccounts.BadPayee.selector);
        ch.open(root, address(ch), address(usdc), 100, 60, 7, "", 0);
        vm.prank(signer);
        vm.expectRevert(AgentAccounts.BadPayee.selector);
        ch.open(root, address(pools), address(usdc), 100, 60, 7, "", 0);
        vm.prank(signer);
        vm.expectRevert(AgentAccounts.BadPayee.selector);
        acc.transfer(root, address(usdc), address(pools), 100, "", 0);
        vm.prank(signer);
        ch.open(root, address(usdc), address(usdc), 100, 60, 8, "", 0); // any other contract is allowed
    }

    // VERIFY-2 storage/ABI: the new fields round-trip through the getters and the constructor-style
    // positional Claim literal in join lands each field in the right slot.
    function test_V2_new_fields_round_trip() public {
        bytes32 id = _open(123, 45, 9);
        PaymentChannels.Channel memory c = ch.get(id);
        assertEq(c.payer, root); assertEq(c.signer, signer); assertEq(c.payee, payee);
        assertEq(c.token, address(usdc)); assertEq(c.deposit, 123); assertEq(c.paid, 0);
        assertEq(c.seq, 0); assertEq(c.timeoutSecs, 45); assertEq(c.closingAt, 0);
        assertFalse(c.closed); assertTrue(c.exists);
        _join(pid, root, signer, 77);
        Pools.Claim memory k = pools.claimOf(pid, root);
        assertEq(k.deposit, 77); assertEq(k.paid, 0); assertEq(k.seq, 0); assertEq(k.exitAt, 0);
        assertEq(k.epoch, 1); assertEq(k.signer, signer); assertFalse(k.exited); assertTrue(k.exists);
    }
}
