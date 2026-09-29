// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.30;

import {Script, console} from "forge-std/Script.sol";
import {AgentAccounts} from "../src/AgentAccounts.sol";
import {PaymentChannels} from "../src/PaymentChannels.sol";
import {Pools} from "../src/Pools.sol";

/// Deploys the three contracts and locks the module set. Run with:
///   forge script script/Deploy.s.sol --rpc-url $RPC --broadcast --private-key $PK
/// No admin keys remain after this: the deployer's only power (lockModules) is spent here.
contract Deploy is Script {
    function run() external returns (AgentAccounts accounts, PaymentChannels channels, Pools pools) {
        vm.startBroadcast();
        accounts = new AgentAccounts();
        channels = new PaymentChannels(accounts);
        pools = new Pools(accounts);
        address[] memory mods = new address[](2);
        mods[0] = address(channels);
        mods[1] = address(pools);
        accounts.lockModules(mods);
        vm.stopBroadcast();
        console.log("AgentAccounts   ", address(accounts));
        console.log("PaymentChannels ", address(channels));
        console.log("Pools           ", address(pools));
    }
}
