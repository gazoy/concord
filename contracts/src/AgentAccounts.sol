// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

/// @title Foliant agent accounts: spending policies enforced by the ledger, in a tree.
/// @notice Port of foliant/accounts.py and the account half of foliant/ledger.py.
///
/// An account is operated by a `signer` under a `Policy`. Every unit of value that
/// leaves the account's tree is checked against the policy of the account and of
/// every ancestor, and recorded in every ancestor's spend window, all or nothing.
/// Moves inside a tree (delegate down, recall up) are not spends.
///
/// Differences from the Python reference, all deliberate:
///  * signers are EVM addresses and call the contract directly, so the signed
///    envelope and account nonce are replaced by msg.sender and the tx nonce;
///  * value is any ERC-20, held here in per-account internal balances;
///  * payees are addresses; the only moves inside a tree are delegate and recall;
///  * allow and deny lists are bounded arrays (MAX_LIST) so `within` can compare them;
///  * escalation is an EIP-712 signature by the policy's co-signer over the exact
///    spend, replay-protected by a per-account escalation nonce and a deadline;
///  * the spend window is a fixed ring of SLOTS time buckets rather than a list of
///    entries, so the cost of a spend is independent of how many spends preceded it
///    (AUDIT-1 A1-1). Each bucket spans bucketLen = ceil(windowSecs / (SLOTS-1)) seconds
///    and is live while its end is inside the window, so the ring's period
///    (SLOTS * bucketLen) always exceeds windowSecs and a reused slot is stale. Value
///    counts for at least windowSecs and, while the window is unchanged, at most
///    windowSecs + bucketLen - 1: the on-chain window can only be more conservative
///    than the reference (A1-9, A1-10). When an administrator changes windowSecs, a
///    live bucket may be merged under a later end: value recorded at t under (W0, L0)
///    is counted until at most t + (L0 - 1) + sum over changes of (W_i + L_i - 1) + W_final,
///    and never until less than t + W_final (AUDIT-1 section 8);
///  * payees may not be this contract or the zero address (A1-3); deposits and
///    refunds credit the amount actually received (A1-2).
contract AgentAccounts is ReentrancyGuard, EIP712 {
    using SafeERC20 for IERC20;

    // ------------------------------------------------------------------ types

    struct PolicyInput {
        uint128 perTxMax;
        uint128 perWindowMax;
        uint32 windowSecs;
        bool hasAllowList;      // false = any payee
        address[] allowList;
        address[] denyList;
        uint64 expiry;          // unix seconds; 0 = never
        address escalation;     // co-signer that may lift perTxMax; 0 = none
    }

    struct Policy {
        uint128 perTxMax;
        uint128 perWindowMax;
        uint32 windowSecs;
        bool hasAllowList;
        uint64 expiry;
        address escalation;
        address[] allowList;
        address[] denyList;
    }

    /// One time bucket of the spend window: the bucket's nominal end (last second) and the total recorded.
    /// A slot is live while `end > now - windowSecs`; a spend at t <= end therefore counts for at least
    /// windowSecs after t, and at most bucketLen - 1 seconds longer (over-count only).
    struct Slot {
        uint64 end;
        uint128 amount;
    }

    struct Account {
        address owner;
        address signer;
        bytes32 parent;         // 0 = root
        uint64 escalationNonce;
        bool exists;
        Policy policy;
        Slot[32] window;        // bucketed record of committed value (SLOTS)
    }

    // -------------------------------------------------------------- constants

    uint256 public constant MAX_LIST = 32;
    uint256 public constant SLOTS = 32;
    uint256 public constant MAX_AMOUNT = type(uint128).max;
    uint256 public constant MAX_DEPTH = 16;
    bytes32 public constant ESCALATION_TYPEHASH =
        keccak256("Escalation(bytes32 account,address token,address payee,uint256 amount,uint64 nonce,uint64 deadline)");

    // ---------------------------------------------------------------- storage

    mapping(bytes32 => Account) private _accounts;
    mapping(bytes32 => mapping(address => uint256)) public balanceOf; // account id => token => amount
    mapping(address => bool) public isModule;                         // channel/pool contracts that may commit spend
    address private _deployer;
    bool public modulesLocked;

    // ----------------------------------------------------------------- events

    event Registered(bytes32 indexed id, address indexed owner, address indexed signer, bytes32 parent);
    event Deposited(bytes32 indexed id, address indexed token, uint256 amount, address from);
    event Refunded(bytes32 indexed id, address indexed token, uint256 amount, address module);
    event EscalationRevoked(bytes32 indexed id, address by, uint64 newNonce);
    event Spent(bytes32 indexed id, address indexed token, address indexed payee, uint256 amount, bool escalated);
    event Delegated(bytes32 indexed parent, bytes32 indexed child, address token, uint256 fund);
    event Recalled(bytes32 indexed parent, bytes32 indexed child, address token, uint256 amount);
    event PolicySet(bytes32 indexed id, address by);
    event SignerRotated(bytes32 indexed id, address newSigner, address by);
    event ModulesLocked(address[] modules);

    // ----------------------------------------------------------------- errors

    error AccountExists();
    error NoAccount();
    error Unauthorized();
    error BadPolicy(string reason);
    error PolicyViolation(string reason);
    error InsufficientFunds();
    error ZeroAmount();
    error NotDescendant();
    error ListTooLong();
    error DepthExceeded();
    error ModulesAlreadyLocked();
    error BadEscalation();
    error BadPayee();

    constructor() EIP712("FoliantAgentAccounts", "1") {
        _deployer = msg.sender;
    }

    // --------------------------------------------------------------- modules

    /// @notice One-shot: name the channel and pool contracts allowed to commit spend, then lock.
    function lockModules(address[] calldata modules) external {
        if (msg.sender != _deployer || modulesLocked) revert ModulesAlreadyLocked();
        for (uint256 i = 0; i < modules.length; i++) isModule[modules[i]] = true;
        modulesLocked = true;
        emit ModulesLocked(modules);
    }

    // ------------------------------------------------------------ registration

    function accountId(address owner, address signer, uint256 salt) public pure returns (bytes32) {
        return keccak256(abi.encode("account", owner, signer, salt));
    }

    function childId(bytes32 parent, address signer, uint256 salt) public pure returns (bytes32) {
        return keccak256(abi.encode("child", parent, signer, salt));
    }

    /// @notice The owner registers a root account operated by `signer` under `policy`.
    function register(address signer, PolicyInput calldata policy, uint256 salt) external returns (bytes32 id) {
        id = accountId(msg.sender, signer, salt);
        if (_accounts[id].exists) revert AccountExists();
        _validatePolicy(policy);
        Account storage a = _accounts[id];
        a.owner = msg.sender;
        a.signer = signer;
        a.exists = true;
        _storePolicy(a.policy, policy);
        emit Registered(id, msg.sender, signer, bytes32(0));
    }

    /// @notice Anyone may fund an account. Funding is not a spend.
    function deposit(bytes32 id, address token, uint256 amount) external nonReentrant {
        if (!_accounts[id].exists) revert NoAccount();
        if (amount == 0) revert ZeroAmount();
        uint256 received = _pull(token, msg.sender, amount);
        balanceOf[id][token] += received;
        emit Deposited(id, token, received, msg.sender);
    }

    // ----------------------------------------------------------------- spends

    /// @notice Pay `amount` of `token` to an address outside the tree. Signer only.
    /// @param escalationSig empty, or the policy's escalation co-signer's EIP-712 signature over this spend,
    ///        the account's current escalation nonce and `escalationDeadline`.
    function transfer(
        bytes32 id,
        address token,
        address payee,
        uint256 amount,
        bytes calldata escalationSig,
        uint64 escalationDeadline
    ) external nonReentrant {
        Account storage a = _account(id);
        if (msg.sender != a.signer) revert Unauthorized();
        _requirePayee(payee);
        _requireFunds(id, token, amount);
        bool escalated = _checkEscalation(a, id, token, payee, amount, escalationSig, escalationDeadline);
        _authorise(id, a, token, payee, amount, escalated);
        balanceOf[id][token] -= amount;
        IERC20(token).safeTransfer(payee, amount);
    }

    /// @notice Called by a locked module (channel or pool contract) to commit `amount` of the
    /// account's balance to `payee` on behalf of `caller`, who must be the account's signer.
    /// The value is moved to the module, which holds it in escrow.
    /// @dev Trust boundary (A1-7): the module asserts `caller`; modules are a locked set and must
    /// authenticate the signer themselves.
    function commit(
        bytes32 id,
        address caller,
        address token,
        address payee,
        uint256 amount,
        bytes calldata escalationSig,
        uint64 escalationDeadline
    ) external nonReentrant {
        if (!isModule[msg.sender]) revert Unauthorized();
        Account storage a = _account(id);
        if (caller != a.signer) revert Unauthorized();
        _requirePayee(payee);
        _requireFunds(id, token, amount);
        bool escalated = _checkEscalation(a, id, token, payee, amount, escalationSig, escalationDeadline);
        _authorise(id, a, token, payee, amount, escalated);
        balanceOf[id][token] -= amount;
        IERC20(token).safeTransfer(msg.sender, amount);
    }

    /// @notice A module returns unspent escrow to an account (channel close, pool exit). Not a spend.
    function refund(bytes32 id, address token, uint256 amount) external nonReentrant {
        if (!isModule[msg.sender]) revert Unauthorized();
        if (!_accounts[id].exists) revert NoAccount();
        if (amount == 0) revert ZeroAmount();
        uint256 received = _pull(token, msg.sender, amount);
        balanceOf[id][token] += received;
        emit Refunded(id, token, received, msg.sender);
    }

    // ------------------------------------------------------------------- tree

    /// @notice Create a child account under `parent` with a policy within the parent's,
    /// and move `fund` of `token` down to it. Funding a child is not a spend. Parent's signer only.
    function delegate(
        bytes32 parent,
        address signer,
        PolicyInput calldata policy,
        uint256 salt,
        address token,
        uint256 fund
    ) external returns (bytes32 id) {
        Account storage p = _account(parent);
        if (msg.sender != p.signer) revert Unauthorized();
        if (_depth(parent) + 1 >= MAX_DEPTH) revert DepthExceeded();
        _validatePolicy(policy);
        _within(policy, p.policy);
        id = childId(parent, signer, salt);
        if (_accounts[id].exists) revert AccountExists();
        Account storage c = _accounts[id];
        c.owner = p.owner;
        c.signer = signer;
        c.parent = parent;
        c.exists = true;
        _storePolicy(c.policy, policy);
        if (fund > 0) {
            if (balanceOf[parent][token] < fund) revert InsufficientFunds();
            balanceOf[parent][token] -= fund;
            balanceOf[id][token] += fund;
        }
        emit Registered(id, p.owner, signer, parent);
        emit Delegated(parent, id, token, fund);
    }

    /// @notice Pull a descendant's free balance back up. Not a spend. Signer of `id` only.
    /// @param amount 0 = everything.
    function recall(bytes32 id, bytes32 child, address token, uint256 amount) external {
        Account storage a = _account(id);
        if (msg.sender != a.signer) revert Unauthorized();
        if (!isDescendant(child, id)) revert NotDescendant();
        uint256 amt = amount == 0 ? balanceOf[child][token] : amount;
        if (balanceOf[child][token] < amt) revert InsufficientFunds();
        balanceOf[child][token] -= amt;
        balanceOf[id][token] += amt;
        emit Recalled(id, child, token, amt);
    }

    // ---------------------------------------------------------- administration

    /// @notice Owner or any ancestor's signer. A child's policy must stay within its parent's.
    function setPolicy(bytes32 id, PolicyInput calldata policy) external {
        Account storage a = _account(id);
        if (!_mayAdminister(a, msg.sender)) revert Unauthorized();
        _validatePolicy(policy);
        if (a.parent != bytes32(0)) _within(policy, _accounts[a.parent].policy);
        _storePolicy(a.policy, policy);
        emit PolicySet(id, msg.sender);
    }

    /// @notice Owner or any ancestor's signer.
    function rotateSigner(bytes32 id, address newSigner) external {
        Account storage a = _account(id);
        if (!_mayAdminister(a, msg.sender)) revert Unauthorized();
        a.signer = newSigner;
        emit SignerRotated(id, newSigner, msg.sender);
    }

    // ------------------------------------------------------------------ views

    function exists(bytes32 id) external view returns (bool) { return _accounts[id].exists; }
    function ownerOf(bytes32 id) external view returns (address) { return _account(id).owner; }
    function signerOf(bytes32 id) external view returns (address) { return _account(id).signer; }
    function parentOf(bytes32 id) external view returns (bytes32) { return _account(id).parent; }
    function escalationNonce(bytes32 id) external view returns (uint64) { return _account(id).escalationNonce; }

    function policyOf(bytes32 id) external view returns (PolicyInput memory p) {
        Policy storage s = _account(id).policy;
        p.perTxMax = s.perTxMax;
        p.perWindowMax = s.perWindowMax;
        p.windowSecs = s.windowSecs;
        p.hasAllowList = s.hasAllowList;
        p.allowList = s.allowList;
        p.denyList = s.denyList;
        p.expiry = s.expiry;
        p.escalation = s.escalation;
    }

    /// @notice Value committed by `id` inside its current window (bucketed; see the header note).
    function spentInWindow(bytes32 id) public view returns (uint256 total) {
        Account storage a = _account(id);
        uint256 cutoff = _cutoff(a.policy.windowSecs);
        for (uint256 i = 0; i < SLOTS; i++) {
            Slot storage sl = a.window[i];
            if (sl.end > cutoff) total += sl.amount;
        }
    }

    function isDescendant(bytes32 id, bytes32 ancestor) public view returns (bool) {
        bytes32 p = _account(id).parent;
        while (p != bytes32(0)) {
            if (p == ancestor) return true;
            p = _accounts[p].parent;
        }
        return false;
    }

    function escalationDigest(bytes32 id, address token, address payee, uint256 amount, uint64 nonce, uint64 deadline)
        public
        view
        returns (bytes32)
    {
        return _hashTypedDataV4(keccak256(abi.encode(ESCALATION_TYPEHASH, id, token, payee, amount, nonce, deadline)));
    }

    /// @notice Invalidate any outstanding escalation signature for `id`. Signer, owner, an ancestor's
    /// signer, or the policy's co-signer itself (A1-11).
    function revokeEscalation(bytes32 id) external {
        Account storage a = _account(id);
        if (msg.sender != a.signer && msg.sender != a.policy.escalation && !_mayAdminister(a, msg.sender)) {
            revert Unauthorized();
        }
        a.escalationNonce += 1;
        emit EscalationRevoked(id, msg.sender, a.escalationNonce);
    }

    // -------------------------------------------------------------- internals

    function _account(bytes32 id) internal view returns (Account storage a) {
        a = _accounts[id];
        if (!a.exists) revert NoAccount();
    }

    function _requireFunds(bytes32 id, address token, uint256 amount) internal view {
        if (amount == 0) revert ZeroAmount();
        if (amount > MAX_AMOUNT) revert PolicyViolation("amount too large"); // window slots are uint128
        if (balanceOf[id][token] < amount) revert InsufficientFunds();
    }

    function _requirePayee(address payee) internal view {
        if (payee == address(0) || payee == address(this) || isModule[payee]) revert BadPayee();
    }

    /// @dev Pull `amount` of `token` from `from`; return what actually arrived (fee-on-transfer safe).
    function _pull(address token, address from, uint256 amount) internal returns (uint256) {
        uint256 before = IERC20(token).balanceOf(address(this));
        IERC20(token).safeTransferFrom(from, address(this), amount);
        return IERC20(token).balanceOf(address(this)) - before;
    }

    function _cutoff(uint32 windowSecs) internal view returns (uint256) {
        return block.timestamp > windowSecs ? block.timestamp - windowSecs : 0;
    }

    function _depth(bytes32 id) internal view returns (uint256 d) {
        bytes32 p = _accounts[id].parent;
        while (p != bytes32(0)) { d++; p = _accounts[p].parent; }
    }

    function _mayAdminister(Account storage a, address key) internal view returns (bool) {
        if (key == a.owner) return true;
        bytes32 p = a.parent;
        while (p != bytes32(0)) {
            if (_accounts[p].signer == key) return true;
            p = _accounts[p].parent;
        }
        return false;
    }

    /// @dev Escalation lifts perTxMax for this account only. The co-signer signs the exact spend, the
    /// account's current escalation nonce and a deadline. The nonce advances here; because a failed spend
    /// reverts the whole call, it is consumed only when the spend succeeds.
    function _checkEscalation(
        Account storage a,
        bytes32 id,
        address token,
        address payee,
        uint256 amount,
        bytes calldata sig,
        uint64 deadline
    ) internal returns (bool) {
        if (sig.length == 0) return false;
        if (a.policy.escalation == address(0)) revert BadEscalation();
        if (block.timestamp > deadline) revert BadEscalation(); // a deadline of 0 is always expired
        bytes32 digest = escalationDigest(id, token, payee, amount, a.escalationNonce, deadline);
        (address rec, ECDSA.RecoverError err,) = ECDSA.tryRecover(digest, sig);
        if (err != ECDSA.RecoverError.NoError || rec != a.policy.escalation) revert BadEscalation();
        a.escalationNonce += 1;
        return true;
    }

    /// @dev Value leaving the tree: every ancestor's policy must allow it, then every ancestor's window
    /// records it. All-or-nothing by revert. `escalated` applies to `id` alone, never to ancestors.
    function _authorise(bytes32 id, Account storage a, address token, address payee, uint256 amount, bool escalated)
        internal
    {
        // walk up, checking
        bytes32 cur = id;
        Account storage node = a;
        while (true) {
            _check(node.policy, payee, amount, spentInWindowOf(node), escalated && cur == id);
            if (node.parent == bytes32(0)) break;
            cur = node.parent;
            node = _accounts[cur];
        }
        // walk up, recording
        cur = id;
        node = a;
        while (true) {
            _record(node, amount);
            if (node.parent == bytes32(0)) break;
            cur = node.parent;
            node = _accounts[cur];
        }
        emit Spent(id, token, payee, amount, escalated);
    }

    function spentInWindowOf(Account storage a) internal view returns (uint256 total) {
        uint256 cutoff = _cutoff(a.policy.windowSecs);
        for (uint256 i = 0; i < SLOTS; i++) {
            Slot storage sl = a.window[i];
            if (sl.end > cutoff) total += sl.amount;
        }
    }

    /// @dev Add `amount` to the bucket for now. bucketLen = ceil(W / (SLOTS-1)), so (SLOTS-1) * bucketLen >= W
    /// and the slot for now was last used more than W seconds ago whenever it is not the same bucket: a
    /// live slot is only ever this bucket, or a bucket left over from a longer earlier window (after a
    /// policy change), in which case the amounts merge under the later end. Either way nothing recorded
    /// stops counting before W seconds have passed.
    function _record(Account storage a, uint256 amount) internal {
        uint256 w = a.policy.windowSecs;
        uint256 bucketLen = (w + SLOTS - 2) / (SLOTS - 1); // ceil(w / (SLOTS-1)), >= 1
        uint256 bucket = block.timestamp / bucketLen;
        // slither-disable-next-line divide-before-multiply  (floor to the bucket start, intended)
        uint64 curEnd = uint64(bucket * bucketLen + bucketLen - 1);
        // slither-disable-next-line weak-prng  (a bucket index, not randomness)
        Slot storage sl = a.window[bucket % SLOTS];
        if (sl.end > _cutoff(uint32(w))) {
            if (curEnd > sl.end) sl.end = curEnd;
            // amount <= MAX_AMOUNT (checked in _requireFunds); sum bounded by perWindowMax (uint128) after _check
            // forge-lint: disable-next-line(unsafe-typecast)
            sl.amount += uint128(amount);
        } else {
            sl.end = curEnd;
            // forge-lint: disable-next-line(unsafe-typecast)
            sl.amount = uint128(amount);
        }
    }

    /// @dev Policy.check from the reference, same order and same conditions.
    function _check(Policy storage p, address payee, uint256 amount, uint256 spent, bool escalated) internal view {
        if (p.expiry != 0 && block.timestamp >= p.expiry) revert PolicyViolation("policy expired");
        if (_contains(p.denyList, payee)) revert PolicyViolation("payee is denied");
        if (p.hasAllowList && !_contains(p.allowList, payee)) revert PolicyViolation("payee is not on the allow list");
        if (amount > p.perTxMax && !escalated) revert PolicyViolation("amount exceeds per_tx_max");
        if (spent + amount > p.perWindowMax) revert PolicyViolation("amount would exceed per_window_max");
    }

    /// @dev Policy.within from the reference: child no wider than parent on every compared axis.
    /// windowSecs and escalation are deliberately not compared.
    function _within(PolicyInput calldata c, Policy storage p) internal view {
        if (c.perTxMax > p.perTxMax) revert BadPolicy("child per_tx_max exceeds parent");
        if (c.perWindowMax > p.perWindowMax) revert BadPolicy("child per_window_max exceeds parent");
        if (p.hasAllowList) {
            if (!c.hasAllowList) revert BadPolicy("child allow list must be a subset of the parent's");
            for (uint256 i = 0; i < c.allowList.length; i++) {
                if (!_contains(p.allowList, c.allowList[i])) revert BadPolicy("child allow list must be a subset of the parent's");
            }
        }
        for (uint256 i = 0; i < p.denyList.length; i++) {
            if (!_containsCalldata(c.denyList, p.denyList[i])) revert BadPolicy("child deny list must include the parent's");
        }
        if (p.expiry != 0 && (c.expiry == 0 || c.expiry > p.expiry)) revert BadPolicy("child expiry must not be later than the parent's");
    }

    function _validatePolicy(PolicyInput calldata p) internal pure {
        if (p.allowList.length > MAX_LIST || p.denyList.length > MAX_LIST) revert ListTooLong();
        if (p.windowSecs == 0) revert BadPolicy("window_secs must be positive");
    }

    function _storePolicy(Policy storage s, PolicyInput calldata p) internal {
        s.perTxMax = p.perTxMax;
        s.perWindowMax = p.perWindowMax;
        s.windowSecs = p.windowSecs;
        s.hasAllowList = p.hasAllowList;
        s.expiry = p.expiry;
        s.escalation = p.escalation;
        delete s.allowList;
        delete s.denyList;
        if (p.hasAllowList) for (uint256 i = 0; i < p.allowList.length; i++) s.allowList.push(p.allowList[i]);
        for (uint256 i = 0; i < p.denyList.length; i++) s.denyList.push(p.denyList[i]);
    }

    function _contains(address[] storage list, address x) internal view returns (bool) {
        for (uint256 i = 0; i < list.length; i++) if (list[i] == x) return true;
        return false;
    }

    function _containsCalldata(address[] calldata list, address x) internal pure returns (bool) {
        for (uint256 i = 0; i < list.length; i++) if (list[i] == x) return true;
        return false;
    }
}
