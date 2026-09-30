# Foliant contracts

Solidity port of the reference implementation in `../foliant/`, built with Foundry and audited
independently as it was written (`audits/`). Three contracts, no admin keys after deployment.

| Contract | Reference | What it does |
| --- | --- | --- |
| `AgentAccounts` | `accounts.py`, account half of `ledger.py` | Accounts with a spending policy (per-tx cap, per-window cap, allow/deny lists, expiry, escalation co-signer) in a tree: delegate budget down, recall it up; every unit leaving the tree is checked against every ancestor's policy and window. ERC-20 balances. |
| `PaymentChannels` | `channels.py` | One payer account, one payee; off-chain EIP-712 updates with monotonic balance; anyone settles; the payer closes with a timeout the payee can contest. |
| `Pools` | `pools.py` | Many payer accounts, one coordinator; one settlement for every member; unilateral exit with a contest window. |

Audits: [AUDIT-1](audits/AUDIT-1.md) (accounts, 12 findings, three rounds) and [AUDIT-2](audits/AUDIT-2.md)
(channels and pools, 9 findings, two rounds), all closed. Two of the AUDIT-2 findings were bugs in
the protocol itself and were fixed in the Python reference in step: updates are ordered by balance,
not by a payer-chosen sequence number, and pool claims carry an epoch so an update from an earlier
membership cannot replay.

Tests: 102 (unit tests mirroring the Python suites, fuzz, stateful invariants across all three
contracts, and a 5,000-run comparison of the on-chain spend window against the reference).
Slither: nothing above informational.

## Build and test

```
curl -L https://foundry.paradigm.xyz | bash && foundryup   # once
./setup.sh                                                 # dependencies at the audited versions
forge test
```

## Deploy

```
export PK=0x...                       # deployer key; its only power (locking the module set) is spent at deploy
forge script script/Deploy.s.sol --rpc-url fuji --broadcast --private-key $PK
```

Prints the three addresses. `--verify --etherscan-api-key $SNOWTRACE_KEY` verifies source on Snowtrace.

`script/Crew.s.sol` runs the reference's crew scenario against a deployment (an orchestrator account,
a delegated worker, a pool deposit, a worker channel settled by the provider, a recall) and prints the
balances and windows; see its header for the environment variables.

## Gas (Foundry, optimizer 200 runs)

| Operation | Gas |
| --- | ---: |
| register | ~150k |
| deposit | ~65k |
| transfer (policy walk, depth 1) | ~120k |
| delegate | ~215k |
| recall | ~35k |
| channel open | ~325k |
| channel settle | ~100k |
| channel close + finalize | ~90k |
| pool create | ~70k |
| pool join | ~250k |
| pool settle (3 members) | ~190k |
| pool exit + finalize | ~190k |

A session of N calls costs one open/join and one settle regardless of N.

## Deployments

| Network | AgentAccounts | PaymentChannels | Pools |
| --- | --- | --- | --- |
| Fuji (43113) | [`0xDB6940FBD9Dcf8AAD1aBB521B4b6790D0b579c92`](https://testnet.snowtrace.io/address/0xDB6940FBD9Dcf8AAD1aBB521B4b6790D0b579c92) | [`0xa857d5EF74F63fd10BD786bf2d66EaA53736c976`](https://testnet.snowtrace.io/address/0xa857d5EF74F63fd10BD786bf2d66EaA53736c976) | [`0x26665c7Ad0a4272F87D1949745dD4B4743F53662`](https://testnet.snowtrace.io/address/0x26665c7Ad0a4272F87D1949745dD4B4743F53662) |

Deployed 30 Sep 2026 at block 58893255; modules locked in the same block (deployer holds no further power).

## Not in this version

Streams (§6.6), the coordinator's liveness bond, ERC-1271 contract signers, rebasing tokens.
A rotation of an account's signer ends payment through its open channels and pool claims (the new
signer closes/exits and reopens/rejoins); updates the payee already holds stay valid.
