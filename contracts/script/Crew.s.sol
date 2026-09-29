// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Script, console} from "forge-std/Script.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {PaymentChannels} from "../src/PaymentChannels.sol";
import {Pools} from "../src/Pools.sol";
import {MockERC20} from "../test/MockERC20.sol";

/// Scenario E from demo/run_demo.py (the crew), against deployed contracts on a live chain.
/// Env: ACCOUNTS, CHANNELS, POOLS (addresses), TOKEN (a mintable MockERC20; deployed if unset),
/// PK (orchestrator/owner key), WORKER_PK, PROVIDER (payee address). Everything is signed by
/// keys the caller holds; the script never needs anyone else's key.
contract Crew is Script {
    AgentAccounts acc;
    PaymentChannels ch;
    Pools pools;
    MockERC20 token;
    uint256 pk;
    uint256 wpk;
    address provider;

    function run() external {
        acc = AgentAccounts(vm.envAddress("ACCOUNTS"));
        ch = PaymentChannels(vm.envAddress("CHANNELS"));
        pools = Pools(vm.envAddress("POOLS"));
        pk = vm.envUint("PK");
        wpk = vm.envUint("WORKER_PK");
        provider = vm.envAddress("PROVIDER");
        address me = vm.addr(pk);
        address worker = vm.addr(wpk);

        vm.startBroadcast(pk);
        token = vm.envOr("TOKEN", address(0)) == address(0) ? new MockERC20() : MockERC20(vm.envAddress("TOKEN"));
        // 1. orchestrator account: 500 per tx, 2000 per hour
        AgentAccounts.PolicyInput memory p;
        p.perTxMax = 500e6; p.perWindowMax = 2000e6; p.windowSecs = 3600;
        bytes32 root = acc.register(me, p, block.timestamp);
        token.mint(me, 10_000e6);
        token.approve(address(acc), 10_000e6);
        acc.deposit(root, address(token), 10_000e6);
        // 2. delegate a worker with a tighter policy and 1000 of funding (not a spend)
        AgentAccounts.PolicyInput memory wp;
        wp.perTxMax = 100e6; wp.perWindowMax = 300e6; wp.windowSecs = 3600;
        bytes32 w = acc.delegate(root, worker, wp, 0, address(token), 1000e6);
        // 3. the orchestrator joins the provider's pool
        bytes32 pid = pools.create(address(token), 3600, block.timestamp); // in the demo the provider creates it; here we stand in
        pools.join(pid, root, 300e6, "", 0);
        vm.stopBroadcast();

        _workerSession(w);
        vm.startBroadcast(pk);
        // 5. the orchestrator recalls the worker's unspent funding (not a spend)
        acc.recall(root, w, address(token), 0);
        vm.stopBroadcast();

        console.log("root spent in window  ", acc.spentInWindow(root) / 1e6);   // 300 (pool) + 100 (worker's channel counted up the tree)
        console.log("worker spent in window", acc.spentInWindow(w) / 1e6);      // 100
        console.log("provider received     ", token.balanceOf(provider) / 1e6); // 9
        console.log("worker balance        ", acc.balanceOf(w, address(token)) / 1e6); // 0 after recall
        console.log("root balance          ", acc.balanceOf(root, address(token)) / 1e6); // 10000 - 300 - 100
    }

    /// 4. the worker opens a channel to the provider for 100 (its per-tx cap) and pays 3 calls of 3
    function _workerSession(bytes32 w) internal {
        vm.startBroadcast(wpk);
        bytes32 cid = ch.open(w, provider, address(token), 100e6, 3600, 0, "", 0);
        vm.stopBroadcast();
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(wpk, ch.updateDigest(cid, w, 3, 9e6));
        vm.startBroadcast(pk);
        ch.settle(cid, 3, 9e6, abi.encodePacked(r, s, v)); // the provider (or anyone) settles the session
        vm.stopBroadcast();
    }
}
