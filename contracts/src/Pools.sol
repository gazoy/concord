// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {AgentAccounts} from "./AgentAccounts.sol";

/// @title Foliant pooled channels: many payer accounts, one coordinator, one settlement.
/// @notice Port of foliant/pools.py (Pool, PoolClaim) and the pool ops of foliant/ledger.py.
///
/// Many payers commit deposits into one pool run by a coordinator (usually the payee). Each
/// payer holds an individually signed, unilaterally exitable claim. Anyone may settle the pool
/// with one transaction carrying each member's latest update; stale updates and exited members
/// are skipped so one member cannot block the batch. If the coordinator disappears, every
/// member's exit still works: start it with the member's latest update, wait `timeoutSecs`
/// (during which anyone may contest with a higher update), then the unspent deposit returns to
/// the member's account.
///
/// Differences from the reference, all deliberate: updates are EIP-712 signatures from the
/// account's current signer; members act through msg.sender == that signer; the coordinator's
/// liveness bond is not ported (its forfeiture is a governance action the reference also leaves
/// out of scope).
contract Pools is ReentrancyGuard, EIP712 {
    using SafeERC20 for IERC20;

    struct Claim {
        uint256 deposit;
        uint256 paid;       // settled to the coordinator so far
        uint64 seq;
        uint64 exitAt;      // 0 = not exiting
        bool exited;
        bool exists;
    }

    struct Pool {
        address coordinator;
        address token;
        uint64 timeoutSecs;
        bool exists;
    }

    struct Update {
        bytes32 account;
        uint64 seq;
        uint256 balance;
        bytes sig;
    }

    bytes32 public constant UPDATE_TYPEHASH =
        keccak256("PoolUpdate(bytes32 pool,bytes32 account,uint64 seq,uint256 balance)");

    AgentAccounts public immutable accounts;
    mapping(bytes32 => Pool) private _pools;
    mapping(bytes32 => mapping(bytes32 => Claim)) private _claims; // pool => account => claim

    event Created(bytes32 indexed id, address indexed coordinator, address token, uint64 timeoutSecs);
    event Joined(bytes32 indexed id, bytes32 indexed account, uint256 deposit);
    event Applied(bytes32 indexed id, bytes32 indexed account, uint64 seq, uint256 balance, uint256 paidOut);
    event Settled(bytes32 indexed id, uint256 total);
    event Exiting(bytes32 indexed id, bytes32 indexed account, uint64 exitAt);
    event Exited(bytes32 indexed id, bytes32 indexed account, uint256 refunded);

    error PoolExists();
    error NoPool();
    error AlreadyMember();
    error NotMember();
    error Unauthorized();
    error StaleUpdate();
    error BadUpdate(string reason);
    error NoExitWindow();
    error TimeoutNotElapsed();
    error ZeroAmount();

    constructor(AgentAccounts accounts_) EIP712("FoliantPools", "1") {
        accounts = accounts_;
    }

    function poolId(address coordinator, uint256 salt) public pure returns (bytes32) {
        return keccak256(abi.encode("pool", coordinator, salt));
    }

    /// @notice The coordinator (msg.sender) creates a pool for `token`.
    function create(address token, uint64 timeoutSecs, uint256 salt) external returns (bytes32 id) {
        id = poolId(msg.sender, salt);
        if (_pools[id].exists) revert PoolExists();
        if (timeoutSecs == 0) revert BadUpdate("timeout must be positive");
        _pools[id] = Pool(msg.sender, token, timeoutSecs, true);
        emit Created(id, msg.sender, token, timeoutSecs);
    }

    /// @notice An account's signer joins with `deposit`, committed under the account's policy with the
    /// coordinator as payee. A member that has exited may join again.
    function join(bytes32 id, bytes32 account, uint256 deposit, bytes calldata escalationSig, uint64 escalationDeadline)
        external
        nonReentrant
    {
        Pool storage p = _pool(id);
        Claim storage c = _claims[id][account];
        if (c.exists && !c.exited) revert AlreadyMember();
        if (deposit == 0) revert ZeroAmount();
        uint256 before = IERC20(p.token).balanceOf(address(this));
        accounts.commit(account, msg.sender, p.token, p.coordinator, deposit, escalationSig, escalationDeadline);
        uint256 received = IERC20(p.token).balanceOf(address(this)) - before;
        _claims[id][account] = Claim(received, 0, 0, 0, false, true);
        emit Joined(id, account, received);
    }

    /// @notice Anyone settles any subset of members with their latest payer-signed updates, paying the
    /// coordinator the total increase in one transfer. Stale updates and exited members are skipped;
    /// a bad signature or an out-of-range balance reverts the batch (as in the reference).
    function settle(bytes32 id, Update[] calldata updates) external nonReentrant returns (uint256 total) {
        Pool storage p = _pool(id);
        for (uint256 i = 0; i < updates.length; i++) {
            Update calldata u = updates[i];
            Claim storage c = _claims[id][u.account];
            if (!c.exists || c.exited) continue;
            _verify(id, u);
            if (u.seq <= c.seq) continue;
            total += _apply(id, c, u.account, u.seq, u.balance);
        }
        if (total > 0) IERC20(p.token).safeTransfer(p.coordinator, total);
        emit Settled(id, total);
    }

    /// @notice A member's signer starts a unilateral exit, optionally applying its latest update first.
    function beginExit(bytes32 id, bytes32 account, uint64 seq, uint256 balance, bytes calldata sig) external nonReentrant {
        Pool storage p = _pool(id);
        Claim storage c = _active(id, account);
        if (msg.sender != accounts.signerOf(account)) revert Unauthorized();
        if (sig.length != 0) {
            _verify(id, Update(account, seq, balance, sig));
            if (seq > c.seq) {
                uint256 out = _apply(id, c, account, seq, balance);
                if (out > 0) IERC20(p.token).safeTransfer(p.coordinator, out);
            }
        }
        if (c.exitAt == 0) {
            c.exitAt = uint64(block.timestamp) + p.timeoutSecs;
            emit Exiting(id, account, c.exitAt);
        }
    }

    /// @notice During a member's exit window anyone may submit a higher update for that member.
    function contestExit(bytes32 id, Update calldata u) external nonReentrant {
        Pool storage p = _pool(id);
        Claim storage c = _active(id, u.account);
        if (c.exitAt == 0 || block.timestamp >= c.exitAt) revert NoExitWindow();
        _verify(id, u);
        if (u.seq <= c.seq) revert StaleUpdate();
        uint256 out = _apply(id, c, u.account, u.seq, u.balance);
        if (out > 0) IERC20(p.token).safeTransfer(p.coordinator, out);
    }

    /// @notice After the exit timeout, the member's signer returns the unspent deposit to the account.
    function finalizeExit(bytes32 id, bytes32 account) external nonReentrant {
        Pool storage p = _pool(id);
        Claim storage c = _active(id, account);
        if (msg.sender != accounts.signerOf(account)) revert Unauthorized();
        if (c.exitAt == 0 || block.timestamp < c.exitAt) revert TimeoutNotElapsed();
        c.exited = true;
        uint256 remaining = c.deposit - c.paid;
        if (remaining > 0) {
            IERC20(p.token).forceApprove(address(accounts), remaining);
            accounts.refund(account, p.token, remaining);
        }
        emit Exited(id, account, remaining);
    }

    // ------------------------------------------------------------------ views

    function get(bytes32 id) external view returns (Pool memory) { return _pool(id); }
    function claimOf(bytes32 id, bytes32 account) external view returns (Claim memory) { return _claims[id][account]; }

    function updateDigest(bytes32 id, bytes32 account, uint64 seq, uint256 balance) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(UPDATE_TYPEHASH, id, account, seq, balance)));
    }

    // -------------------------------------------------------------- internals

    function _pool(bytes32 id) internal view returns (Pool storage p) {
        p = _pools[id];
        if (!p.exists) revert NoPool();
    }

    function _active(bytes32 id, bytes32 account) internal view returns (Claim storage c) {
        c = _claims[id][account];
        if (!c.exists || c.exited) revert NotMember();
    }

    function _verify(bytes32 id, Update memory u) internal view {
        (address rec, ECDSA.RecoverError err,) = ECDSA.tryRecover(updateDigest(id, u.account, u.seq, u.balance), u.sig);
        if (err != ECDSA.RecoverError.NoError || rec != accounts.signerOf(u.account)) revert Unauthorized();
    }

    /// @dev Pool._apply from the reference; returns the amount owed to the coordinator (paid by the caller).
    function _apply(bytes32 id, Claim storage c, bytes32 account, uint64 seq, uint256 balance) internal returns (uint256 delta) {
        if (balance < c.paid || balance > c.deposit) revert BadUpdate("balance out of range");
        delta = balance - c.paid;
        c.seq = seq;
        c.paid = balance;
        emit Applied(id, account, seq, balance, delta);
    }
}
