// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Test, console} from "forge-std/Test.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {MockERC20} from "./MockERC20.sol";

/// Runs docs/spec/vectors.json (spending-policy.md §10) against AgentAccounts: the `check`,
/// `within`, `window` and `tree` kinds. `x402-exact` is a wallet/facilitator binding and has no
/// on-chain counterpart here.
///
/// Where the EVM reference differs from the exact semantics the vectors are written against, the
/// difference is the one the spec allows and is handled explicitly:
///  * windows are approximate (§6.2, B = ceil(W/31)): `spentInWindow` expectations are checked as
///    exact <= got <= everything recorded in the last W + B seconds; vectors marked exactOnly skip;
///  * zero-amount spends are refused before the policy check (§3 note): those vectors skip;
///  * a child can only be funded at delegation, so a tree `fund` op is applied as part of the
///    preceding delegate and the "not a spend" assertions that follow still run against it;
///  * the escalation co-signer in a vector is an arbitrary address; here it is replaced by a key
///    this test holds, so `escalated: true` is a real co-signature (§5), not a flag.
contract SpecVectorsTest is Test {
    string constant PATH = "../docs/spec/vectors.json";
    uint256 constant ESC_KEY = 0xE5CA;
    uint256 constant SIGNER_KEY = 0x5164;
    address constant DUMMY = address(0xD0D0);

    string json;
    AgentAccounts acc;
    MockERC20 token;
    address esc;
    address signer;

    // per-account spend records for the window bound (tree and window kinds)
    mapping(bytes32 => uint256[]) recT;
    mapping(bytes32 => uint256[]) recA;

    function setUp() public {
        json = vm.readFile(PATH);
        esc = vm.addr(ESC_KEY);
        signer = vm.addr(SIGNER_KEY);
    }

    // ------------------------------------------------------------------ driver

    // one test per kind so no single test nears the block gas limit
    function test_check_vectors() public { _run("check", 27, 4); }      // 3 zero-amount + check-030 skipped (header)
    function test_within_vectors() public { _run("within", 22, 0); }
    function test_window_vectors() public { _run("window", 3, 3); }     // 3 exactOnly skipped
    function test_tree_vectors() public { _run("tree", 8, 0); }
    function test_policy_vectors() public { _run("policy", 7, 2); }     // policy-003, policy-009 skipped (header)

    function _run(string memory want, uint256 expectRan, uint256 expectSkipped) internal {
        uint256 ran; uint256 skipped;
        for (uint256 i = 0; ; i++) {
            string memory p = string.concat(".vectors[", vm.toString(i), "]");
            if (!vm.keyExistsJson(json, p)) break;
            string memory kind = vm.parseJsonString(json, string.concat(p, ".kind"));
            if (!eq(kind, want)) continue;
            string memory id = vm.parseJsonString(json, string.concat(p, ".id"));
            bool did;
            if (eq(kind, "check")) did = _check(p, id);
            else if (eq(kind, "within")) did = _within(p, id);
            else if (eq(kind, "window")) did = _window(p, id);
            else if (eq(kind, "tree")) did = _tree(p, id);
            else did = _policyValidity(p, id);
            if (did) ran++; else skipped++;
        }
        console.log(want, "vectors run", ran, skipped);
        assertEq(ran, expectRan, string.concat(want, ": run count"));
        assertEq(skipped, expectSkipped, string.concat(want, ": skip count"));
    }

    // ------------------------------------------------------------------ kinds

    function _fresh() internal {
        acc = new AgentAccounts();
        token = new MockERC20();
    }

    struct CheckV { uint256 amount; address payee; uint256 now_; uint256 spent; bool escalated; string expect; }

    function _check(string memory p, string memory id) internal returns (bool) {
        CheckV memory v;
        v.amount = vm.parseUint(vm.parseJsonString(json, string.concat(p, ".amount")));
        if (v.amount == 0) return false; // ZeroAmount precedes the policy check
        if (eq(id, "check-030")) return false; // mixed-case address text: addresses are bytes here
        AgentAccounts.PolicyInput memory pol = _policy(string.concat(p, ".policy"));
        v.payee = vm.parseJsonAddress(json, string.concat(p, ".payee"));
        v.now_ = vm.parseJsonUint(json, string.concat(p, ".now"));
        v.spent = vm.parseUint(vm.parseJsonString(json, string.concat(p, ".spentInWindow")));
        v.escalated = vm.parseJsonBool(json, string.concat(p, ".escalated"));
        v.expect = vm.parseJsonString(json, string.concat(p, ".expect"));

        _fresh();
        vm.warp(v.now_);
        // a wide policy to pre-load the window with `spent`, then the policy under test
        AgentAccounts.PolicyInput memory wide;
        wide.perTxMax = type(uint128).max; wide.perWindowMax = type(uint128).max; wide.windowSecs = pol.windowSecs;
        bytes32 a = acc.register(signer, wide, 0);
        _deposit(a, v.spent + v.amount);
        if (v.spent > 0) { vm.prank(signer); acc.transfer(a, address(token), DUMMY, v.spent, "", 0); }
        acc.setPolicy(a, pol);
        assertEq(_trySpend(a, SIGNER_KEY, v.payee, v.amount, v.escalated), v.expect, id);
        return true;
    }

    /// Attempt a spend as the account's signer, with a real co-signature when escalated.
    function _trySpend(bytes32 a, uint256 key, address payee, uint256 amt, bool escalated) internal returns (string memory) {
        (bytes memory sig, uint64 dl) = escalated ? _escalate(a, payee, amt) : (bytes(""), uint64(0));
        vm.prank(vm.addr(key));
        (bool ok, bytes memory err) = address(acc).call(abi.encodeCall(acc.transfer, (a, address(token), payee, amt, sig, dl)));
        return _code(ok, err);
    }

    /// `policy` vectors: the wire decoder rejects what it cannot represent (uint128 fields), the
    /// contract rejects what it validates (windowSecs). A wire-form expiry of 0 or a zero-address
    /// co-signer are the contract's own null encodings, indistinguishable from null once parsed by
    /// the cheatcodes, so their rejection is the JSON decoder's, not the contract's: skipped.
    function _policyValidity(string memory p, string memory id) internal returns (bool) {
        if (eq(id, "policy-003") || eq(id, "policy-009")) return false;
        string memory expect = vm.parseJsonString(json, string.concat(p, ".expect"));
        (bool decodable, AgentAccounts.PolicyInput memory pol) = _tryPolicy(string.concat(p, ".policy"));
        if (!decodable) { assertEq(expect, "policy_invalid", id); return true; }
        _fresh();
        (bool ok, bytes memory err) = address(acc).call(abi.encodeCall(acc.register, (signer, pol, 0)));
        string memory got = ok ? "ok" : (bytes4(err) == AgentAccounts.BadPolicy.selector ? "policy_invalid" : _code(ok, err));
        assertEq(got, expect, id);
        return true;
    }

    function _within(string memory p, string memory id) internal returns (bool) {
        AgentAccounts.PolicyInput memory child = _policy(string.concat(p, ".child"));
        AgentAccounts.PolicyInput memory parent = _policy(string.concat(p, ".parent"));
        string memory expect = vm.parseJsonString(json, string.concat(p, ".expect"));
        _fresh();
        bytes32 a = acc.register(signer, parent, 0);
        vm.prank(signer);
        (bool ok, bytes memory err) = address(acc).call(
            abi.encodeCall(acc.delegate, (a, vm.addr(0xC11D), child, 0, address(token), 0)));
        assertEq(_code(ok, err), expect, id);
        return true;
    }

    function _window(string memory p, string memory id) internal returns (bool) {
        if (vm.keyExistsJson(json, string.concat(p, ".exactOnly"))) return false;
        uint32 w = uint32(vm.parseJsonUint(json, string.concat(p, ".windowSecs")));
        _fresh();
        AgentAccounts.PolicyInput memory pol;
        pol.perTxMax = type(uint128).max; pol.perWindowMax = type(uint128).max; pol.windowSecs = w;
        vm.warp(1);
        bytes32 a = acc.register(signer, pol, 0);
        _deposit(a, 1e30);
        for (uint256 i = 0; ; i++) {
            string memory op = string.concat(p, ".ops[", vm.toString(i), "]");
            if (!vm.keyExistsJson(json, op)) break;
            uint256 t = vm.parseJsonUint(json, string.concat(op, ".t"));
            vm.warp(t);
            if (vm.keyExistsJson(json, string.concat(op, ".record"))) {
                uint256 amt = vm.parseUint(vm.parseJsonString(json, string.concat(op, ".record")));
                vm.prank(signer); acc.transfer(a, address(token), DUMMY, amt, "", 0);
                recT[a].push(t); recA[a].push(amt);
            } else if (vm.keyExistsJson(json, string.concat(op, ".setWindow"))) {
                revert("setWindow in a non-exactOnly vector");
            } else {
                uint256 exact = vm.parseUint(vm.parseJsonString(json, string.concat(op, ".expect")));
                _assertWindow(a, w, t, exact, string.concat(id, " op ", vm.toString(i)));
            }
        }
        return true;
    }

    struct Node { string name; bytes32 id; uint256 key; uint32 windowSecs; }

    function _tree(string memory p, string memory id) internal returns (bool) {
        _fresh();
        vm.warp(1);
        Node[] memory nodes = new Node[](8);
        uint256 n;
        for (uint256 i = 0; ; i++) {
            string memory ap = string.concat(p, ".accounts[", vm.toString(i), "]");
            if (!vm.keyExistsJson(json, ap)) break;
            AgentAccounts.PolicyInput memory pol = _policy(string.concat(ap, ".policy"));
            Node memory nd;
            nd.name = vm.parseJsonString(json, string.concat(ap, ".name"));
            nd.key = 0x1000 + i;
            nd.windowSecs = pol.windowSecs;
            bytes memory parentRaw = vm.parseJson(json, string.concat(ap, ".parent"));
            if (_isNull(parentRaw)) {
                nd.id = acc.register(vm.addr(nd.key), pol, i);
                _deposit(nd.id, 1e30);
            } else {
                Node memory par = _node(nodes, n, vm.parseJsonString(json, string.concat(ap, ".parent")));
                uint256 depth = _depthOf(par.id);
                // §4.3: funding at delegation is an internal move. A later `fund` op in the vector
                // is folded in here (see header); the amounts are ample either way.
                uint256 fund = 10 ** (24 - 4 * depth) + _pendingFund(p, nd.name, par.name);
                vm.prank(vm.addr(par.key));
                nd.id = acc.delegate(par.id, vm.addr(nd.key), pol, i, address(token), fund);
            }
            nodes[n++] = nd;
        }
        for (uint256 i = 0; ; i++) {
            string memory op = string.concat(p, ".ops[", vm.toString(i), "]");
            if (!vm.keyExistsJson(json, op)) break;
            uint256 t = vm.parseJsonUint(json, string.concat(op, ".t"));
            vm.warp(t);
            string memory label = string.concat(id, " op ", vm.toString(i));
            if (vm.keyExistsJson(json, string.concat(op, ".spend"))) {
                string memory s = string.concat(op, ".spend");
                Node memory from = _node(nodes, n, vm.parseJsonString(json, string.concat(s, ".from")));
                address payee = vm.parseJsonAddress(json, string.concat(s, ".payee"));
                uint256 amt = vm.parseUint(vm.parseJsonString(json, string.concat(s, ".amount")));
                bool escalated = vm.keyExistsJson(json, string.concat(s, ".escalated"))
                    && vm.parseJsonBool(json, string.concat(s, ".escalated"));
                string memory got = _trySpend(from.id, from.key, payee, amt, escalated);
                assertEq(got, vm.parseJsonString(json, string.concat(op, ".expect")), label);
                if (eq(got, "ok")) _recordUp(from.id, t, amt);
            } else if (vm.keyExistsJson(json, string.concat(op, ".fund"))) {
                // applied at delegation (header); nothing to do now, and nothing was recorded
                assertEq(vm.parseJsonString(json, string.concat(op, ".expect")), "ok", label);
            } else if (vm.keyExistsJson(json, string.concat(op, ".recall"))) {
                string memory r = string.concat(op, ".recall");
                Node memory from = _node(nodes, n, vm.parseJsonString(json, string.concat(r, ".from")));
                Node memory tgt = _node(nodes, n, vm.parseJsonString(json, string.concat(r, ".of")));
                uint256 amt = vm.parseUint(vm.parseJsonString(json, string.concat(r, ".amount")));
                vm.prank(vm.addr(from.key));
                (bool ok, bytes memory err) = address(acc).call(abi.encodeCall(acc.recall, (from.id, tgt.id, address(token), amt)));
                assertEq(_code(ok, err), vm.parseJsonString(json, string.concat(op, ".expect")), label);
            } else if (vm.keyExistsJson(json, string.concat(op, ".setPolicy"))) {
                string memory s = string.concat(op, ".setPolicy");
                string memory by = vm.parseJsonString(json, string.concat(s, ".by"));
                Node memory tgt = _node(nodes, n, vm.parseJsonString(json, string.concat(s, ".of")));
                AgentAccounts.PolicyInput memory pol = _policy(string.concat(s, ".policy"));
                // "owner" is this test (it registered every root); otherwise an ancestor's signer
                if (!eq(by, "owner")) vm.prank(vm.addr(_node(nodes, n, by).key));
                (bool ok, bytes memory err) = address(acc).call(abi.encodeCall(acc.setPolicy, (tgt.id, pol)));
                assertEq(_code(ok, err), vm.parseJsonString(json, string.concat(op, ".expect")), label);
                if (ok) for (uint256 k = 0; k < n; k++) if (nodes[k].id == tgt.id) nodes[k].windowSecs = pol.windowSecs;
            } else if (vm.keyExistsJson(json, string.concat(op, ".spentInWindow"))) {
                Node memory tgt = _node(nodes, n, vm.parseJsonString(json, string.concat(op, ".spentInWindow.of")));
                uint256 exact = vm.parseUint(vm.parseJsonString(json, string.concat(op, ".expect")));
                _assertWindow(tgt.id, tgt.windowSecs, t, exact, label);
            } else {
                revert(string.concat("unknown op in ", label));
            }
        }
        return true;
    }

    // ---------------------------------------------------------------- helpers

    /// §6.2: never under-count; never hold a spend more than B = ceil(W/31) seconds too long.
    function _assertWindow(bytes32 a, uint32 w, uint256 now_, uint256 exact, string memory label) internal view {
        uint256 got = acc.spentInWindow(a);
        assertGe(got, exact, string.concat(label, ": under-count"));
        uint256 b = (uint256(w) + 30) / 31;
        uint256 lo = now_ > uint256(w) + b ? now_ - w - b : 0;
        uint256 upper;
        for (uint256 i = 0; i < recT[a].length; i++) if (recT[a][i] > lo) upper += recA[a][i];
        assertLe(got, upper, string.concat(label, ": over-count beyond bound"));
    }

    function _recordUp(bytes32 id, uint256 t, uint256 amt) internal {
        bytes32 cur = id;
        while (cur != bytes32(0)) {
            recT[cur].push(t); recA[cur].push(amt);
            cur = acc.parentOf(cur);
        }
    }

    function _pendingFund(string memory p, string memory child, string memory parentName) internal view returns (uint256 total) {
        for (uint256 i = 0; ; i++) {
            string memory op = string.concat(p, ".ops[", vm.toString(i), "]");
            if (!vm.keyExistsJson(json, op)) break;
            if (vm.keyExistsJson(json, string.concat(op, ".fund"))
                && eq(vm.parseJsonString(json, string.concat(op, ".fund.to")), child)) {
                assertTrue(eq(vm.parseJsonString(json, string.concat(op, ".fund.from")), parentName),
                    "fund.from must be fund.to's parent (vector format)");
                total += vm.parseUint(vm.parseJsonString(json, string.concat(op, ".fund.amount")));
            }
        }
    }

    function _depthOf(bytes32 id) internal view returns (uint256 d) {
        while (id != bytes32(0)) { d++; id = acc.parentOf(id); }
    }

    function _node(Node[] memory nodes, uint256 n, string memory name) internal pure returns (Node memory) {
        for (uint256 i = 0; i < n; i++) if (eq(nodes[i].name, name)) return nodes[i];
        revert(string.concat("no account ", name));
    }

    function _deposit(bytes32 a, uint256 amount) internal {
        token.mint(address(this), amount);
        token.approve(address(acc), amount);
        acc.deposit(a, address(token), amount);
    }

    function _escalate(bytes32 a, address payee, uint256 amount) internal view returns (bytes memory sig, uint64 dl) {
        dl = uint64(block.timestamp + 100);
        bytes32 digest = _escDigest(a, payee, amount, dl);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(ESC_KEY, digest);
        sig = abi.encodePacked(r, s, v);
    }

    function _escDigest(bytes32 a, address payee, uint256 amount, uint64 dl) internal view returns (bytes32) {
        uint64 nonce = acc.escalationNonce(a);
        return acc.escalationDigest(a, address(token), payee, amount, nonce, dl);
    }

    function _policy(string memory p) internal view returns (AgentAccounts.PolicyInput memory pol) {
        bool okDecode;
        (okDecode, pol) = _tryPolicy(p);
        require(okDecode, "vector policy not representable in uint128/uint32");
    }

    /// Decode a wire-form policy; false when a value does not fit the contract's widths (spec §2:
    /// a binary encoding MUST reject rather than truncate).
    function _tryPolicy(string memory p) internal view returns (bool, AgentAccounts.PolicyInput memory pol) {
        string memory txS = vm.parseJsonString(json, string.concat(p, ".perTxMax"));
        string memory winS = vm.parseJsonString(json, string.concat(p, ".perWindowMax"));
        if (!_isUint(txS) || !_isUint(winS)) return (false, pol);
        uint256 tx_ = vm.parseUint(txS);
        uint256 win = vm.parseUint(winS);
        uint256 w = vm.parseJsonUint(json, string.concat(p, ".windowSecs"));
        if (tx_ > type(uint128).max || win > type(uint128).max || w > type(uint32).max) return (false, pol);
        pol.perTxMax = uint128(tx_);
        pol.perWindowMax = uint128(win);
        pol.windowSecs = uint32(w);
        bytes memory allowRaw = vm.parseJson(json, string.concat(p, ".allowList"));
        if (!_isNull(allowRaw)) {
            pol.hasAllowList = true;
            pol.allowList = vm.parseJsonAddressArray(json, string.concat(p, ".allowList"));
        }
        pol.denyList = vm.parseJsonAddressArray(json, string.concat(p, ".denyList"));
        bytes memory expRaw = vm.parseJson(json, string.concat(p, ".expiry"));
        if (!_isNull(expRaw)) {
            uint256 e = vm.parseJsonUint(json, string.concat(p, ".expiry"));
            if (e > type(uint64).max) return (false, pol);
            pol.expiry = uint64(e);
        }
        bytes memory escRaw = vm.parseJson(json, string.concat(p, ".escalation"));
        if (!_isNull(escRaw)) pol.escalation = esc; // the co-signer this test holds (header)
        return (true, pol);
    }

    /// The spec's amount grammar: ^(0|[1-9][0-9]*)$.
    function _isUint(string memory v) internal pure returns (bool) {
        bytes memory b = bytes(v);
        if (b.length == 0) return false;
        if (b.length > 1 && b[0] == "0") return false;
        for (uint256 i = 0; i < b.length; i++) if (b[i] < "0" || b[i] > "9") return false;
        return true;
    }

    /// A JSON null decodes to a single zero word.
    function _isNull(bytes memory raw) internal pure returns (bool) {
        return raw.length == 32 && bytes32(raw) == bytes32(0);
    }

    /// Map a call result to the spec's reason codes.
    function _code(bool ok, bytes memory err) internal pure returns (string memory) {
        if (ok) return "ok";
        bytes4 sel = bytes4(err);
        if (sel != AgentAccounts.PolicyViolation.selector && sel != AgentAccounts.BadPolicy.selector) {
            return string.concat("unexpected revert ", vm.toString(err));
        }
        bytes memory body = new bytes(err.length - 4);
        for (uint256 i = 0; i < body.length; i++) body[i] = err[i + 4];
        string memory reason = abi.decode(body, (string));
        if (eq(reason, "policy expired")) return "expired";
        if (eq(reason, "payee is denied")) return "payee_denied";
        if (eq(reason, "payee is not on the allow list")) return "payee_not_allowed";
        if (eq(reason, "amount exceeds per_tx_max")) return "per_tx_exceeded";
        if (eq(reason, "amount would exceed per_window_max")) return "per_window_exceeded";
        if (eq(reason, "child per_tx_max exceeds parent")) return "child_per_tx_wider";
        if (eq(reason, "child per_window_max exceeds parent")) return "child_per_window_wider";
        if (eq(reason, "child allow list must be a subset of the parent's")) return "child_allow_wider";
        if (eq(reason, "child deny list must include the parent's")) return "child_deny_narrower";
        if (eq(reason, "child expiry must not be later than the parent's")) return "child_expiry_later";
        return string.concat("unmapped reason: ", reason);
    }

    function eq(string memory a, string memory b) internal pure returns (bool) {
        return keccak256(bytes(a)) == keccak256(bytes(b));
    }
}
