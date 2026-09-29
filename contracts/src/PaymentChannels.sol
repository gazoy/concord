// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {AgentAccounts} from "./AgentAccounts.sol";

/// @title Foliant payment channels: one payer account, one payee, off-chain monotonic updates.
/// @notice Port of foliant/channels.py (Channel) and the channel ops of foliant/ledger.py.
///
/// A channel escrows `deposit` committed from a payer account (through AgentAccounts.commit, so
/// the account's policy tree bounds it). Off-chain the payer's signer signs updates
/// (channel, account, seq, balance) with balance monotonically increasing. Anyone may settle by
/// submitting the latest update; the payer's signer closes by submitting its latest and waiting
/// `timeoutSecs` for the payee to submit a higher one, after which the unspent deposit returns
/// to the payer's account.
///
/// Differences from the reference, all deliberate: updates are EIP-712 signatures from the
/// signer the account had when the channel was opened (snapshotted, so a later rotation cannot
/// void updates the payee already holds — AUDIT-2 A2-4); close authority is the account's
/// current signer; updates are ordered by balance, not by the payer-chosen seq, so a payer
/// cannot block the payee's higher update by jumping seq (A2-1); timeouts are bounded by
/// MAX_TIMEOUT (A2-3); streams (§6.6) are not ported in this version.
contract PaymentChannels is ReentrancyGuard, EIP712 {
    using SafeERC20 for IERC20;

    struct Channel {
        bytes32 payer;      // account id
        address signer;     // the account's signer at open: the key whose updates count
        address payee;
        address token;
        uint256 deposit;
        uint256 paid;       // settled to the payee so far (balance_to_payee)
        uint64 seq;         // seq of the last update applied
        uint64 timeoutSecs;
        uint64 closingAt;   // 0 = not closing
        bool closed;
        bool exists;
    }

    bytes32 public constant UPDATE_TYPEHASH =
        keccak256("ChannelUpdate(bytes32 channel,bytes32 account,uint64 seq,uint256 balance)");

    uint64 public constant MAX_TIMEOUT = 30 days;

    AgentAccounts public immutable accounts;
    mapping(bytes32 => Channel) private _channels;

    event Opened(bytes32 indexed id, bytes32 indexed payer, address indexed payee, address token, uint256 deposit, uint64 timeoutSecs);
    event Settled(bytes32 indexed id, uint64 seq, uint256 balance, uint256 paidOut);
    event Closing(bytes32 indexed id, uint64 closingAt);
    event Closed(bytes32 indexed id, uint256 refunded);

    error ChannelExists();
    error NoChannel();
    error Unauthorized();
    error ChannelClosed();
    error StaleUpdate();
    error BadUpdate(string reason);
    error NotClosing();
    error TimeoutNotElapsed();
    error ZeroAmount();

    constructor(AgentAccounts accounts_) EIP712("FoliantPaymentChannels", "1") {
        accounts = accounts_;
    }

    function channelId(bytes32 payer, address payee, uint256 salt) public pure returns (bytes32) {
        return keccak256(abi.encode("channel", payer, payee, salt));
    }

    /// @notice The payer account's signer opens a channel, committing `deposit` under the account's policy.
    function open(
        bytes32 payer,
        address payee,
        address token,
        uint256 deposit,
        uint64 timeoutSecs,
        uint256 salt,
        bytes calldata escalationSig,
        uint64 escalationDeadline
    ) external nonReentrant returns (bytes32 id) {
        id = channelId(payer, payee, salt);
        if (_channels[id].exists) revert ChannelExists();
        if (deposit == 0) revert ZeroAmount();
        if (timeoutSecs == 0 || timeoutSecs > MAX_TIMEOUT) revert BadUpdate("timeout out of range");
        // commit authenticates msg.sender as the account's signer and enforces the policy tree;
        // the tokens arrive here
        uint256 before = IERC20(token).balanceOf(address(this));
        accounts.commit(payer, msg.sender, token, payee, deposit, escalationSig, escalationDeadline);
        uint256 received = IERC20(token).balanceOf(address(this)) - before;
        Channel storage c = _channels[id];
        c.payer = payer;
        c.signer = msg.sender; // commit has just checked msg.sender is the account's signer
        c.payee = payee;
        c.token = token;
        c.deposit = received;
        c.timeoutSecs = timeoutSecs;
        c.exists = true;
        emit Opened(id, payer, payee, token, received, timeoutSecs);
    }

    /// @notice Anyone submits a payer-signed update; pays the payee the increase. Allowed until closed.
    /// An update is stale if its balance is not higher than what has been settled; seq is carried for
    /// off-chain bookkeeping only.
    function settle(bytes32 id, uint64 seq, uint256 balance, bytes calldata sig) external nonReentrant {
        Channel storage c = _channel(id);
        if (c.closed) revert ChannelClosed();
        _verify(c, id, seq, balance, sig);
        if (balance <= c.paid) revert StaleUpdate();
        _apply(c, id, seq, balance);
    }

    /// @notice The payer's signer starts closing, optionally applying its latest update first.
    /// The payee then has `timeoutSecs` to settle a higher one.
    function beginClose(bytes32 id, uint64 seq, uint256 balance, bytes calldata sig) external nonReentrant {
        Channel storage c = _channel(id);
        if (c.closed) revert ChannelClosed();
        if (msg.sender != accounts.signerOf(c.payer)) revert Unauthorized();
        if (sig.length != 0) {
            _verify(c, id, seq, balance, sig);
            if (balance > c.paid) _apply(c, id, seq, balance);
        }
        if (c.closingAt == 0) {
            c.closingAt = uint64(block.timestamp) + c.timeoutSecs;
            emit Closing(id, c.closingAt);
        }
    }

    /// @notice After the timeout, the payer's signer returns the unspent deposit to the payer account.
    function finalizeClose(bytes32 id) external nonReentrant {
        Channel storage c = _channel(id);
        if (c.closed) revert ChannelClosed();
        if (msg.sender != accounts.signerOf(c.payer)) revert Unauthorized();
        if (c.closingAt == 0) revert NotClosing();
        if (block.timestamp < c.closingAt) revert TimeoutNotElapsed();
        c.closed = true;
        uint256 remaining = c.deposit - c.paid;
        if (remaining > 0) {
            IERC20(c.token).forceApprove(address(accounts), remaining);
            accounts.refund(c.payer, c.token, remaining);
        }
        emit Closed(id, remaining);
    }

    // ------------------------------------------------------------------ views

    function get(bytes32 id) external view returns (Channel memory) { return _channel(id); }

    function updateDigest(bytes32 id, bytes32 account, uint64 seq, uint256 balance) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(UPDATE_TYPEHASH, id, account, seq, balance)));
    }

    // -------------------------------------------------------------- internals

    function _channel(bytes32 id) internal view returns (Channel storage c) {
        c = _channels[id];
        if (!c.exists) revert NoChannel();
    }

    /// @dev verify_update from the reference: signed by the channel's signer (snapshotted at open).
    function _verify(Channel storage c, bytes32 id, uint64 seq, uint256 balance, bytes calldata sig) internal view {
        (address rec, ECDSA.RecoverError err,) = ECDSA.tryRecover(updateDigest(id, c.payer, seq, balance), sig);
        if (err != ECDSA.RecoverError.NoError || rec != c.signer) revert Unauthorized();
    }

    /// @dev _apply_balance from the reference: balance strictly increasing (checked by callers), within deposit.
    function _apply(Channel storage c, bytes32 id, uint64 seq, uint256 balance) internal {
        if (balance > c.deposit) revert BadUpdate("balance exceeds deposit");
        uint256 delta = balance - c.paid;
        c.seq = seq;
        c.paid = balance;
        if (delta > 0) IERC20(c.token).safeTransfer(c.payee, delta);
        emit Settled(id, seq, balance, delta);
    }
}
