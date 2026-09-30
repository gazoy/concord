# Foliant on Avalanche Fuji: measured gas and the cost of a session

30 September 2026. Contracts: AgentAccounts [`0xDB6940FBD9Dcf8AAD1aBB521B4b6790D0b579c92`](https://testnet.snowtrace.io/address/0xDB6940FBD9Dcf8AAD1aBB521B4b6790D0b579c92), PaymentChannels [`0xa857d5EF74F63fd10BD786bf2d66EaA53736c976`](https://testnet.snowtrace.io/address/0xa857d5EF74F63fd10BD786bf2d66EaA53736c976), Pools [`0x26665c7Ad0a4272F87D1949745dD4B4743F53662`](https://testnet.snowtrace.io/address/0x26665c7Ad0a4272F87D1949745dD4B4743F53662), deployed at block 58893255 (four transactions, 6,487,065 gas in total, modules locked in the same block).

## What was run

`contracts/script/Crew.s.sol`, scenario E of the reference demo, against the live contracts: an orchestrator account with a 500/2000-per-hour policy, a worker delegated with a 100/300 policy and 1,000 of funding, the orchestrator joining a provider's pool for 300, the worker opening a 100 channel to the provider and paying three calls of 3 off-chain, the provider settling the channel, and the orchestrator recalling the worker's unspent 900. Every figure below is from the transaction receipts.

| Step | Function | Gas | Transaction |
| --- | --- | ---: | --- |
| Register the orchestrator account | `AgentAccounts.register` | 147,097 | [`0x7b7e…bfff`](https://testnet.snowtrace.io/tx/0x7b7e4a000d62b0aef5b0bd2a4edbc6fd34fe3ddf3ce75ff942f0e899939dbfff) |
| Deposit 10,000 | `AgentAccounts.deposit` | 95,146 | [`0x9397…aaa8`](https://testnet.snowtrace.io/tx/0x9397f1702100d1dc54b32e149b616220b922872abb68c90d9bbcc0026e99aaa8) |
| Delegate a worker (policy within the parent's; fund 1,000, not a spend) | `AgentAccounts.delegate` | 216,369 | [`0x932c…a803`](https://testnet.snowtrace.io/tx/0x932c809c68714cf4a43651d403843ee98894f39e4468cd4f679399a02adba803) |
| Provider creates a pool | `Pools.create` | 69,620 | [`0xb92d…e05b`](https://testnet.snowtrace.io/tx/0xb92d7987f8e82784d9c261299caeab51a23939026b26cf4863a882de4270e05b) |
| Orchestrator joins the pool with 300 (one policy check up the tree) | `Pools.join` | 271,001 | [`0x9f28…da6f`](https://testnet.snowtrace.io/tx/0x9f2862b1a46f599893ac4efd911e0030b909524a9374e9b1639dcbdddf0ada6f) |
| Worker opens a 100 channel (checked against its policy and the orchestrator's) | `PaymentChannels.open` | 417,363 | [`0x5675…df0e`](https://testnet.snowtrace.io/tx/0x5675cf5f8ee043b09e13017e85150d47039ca84e864ecc798f5fad0d9c01df0e) |
| Three calls paid off-chain | (no transaction) | 0 | — |
| Provider settles the channel: 9 arrives | `PaymentChannels.settle` | 107,087 | [`0x2642…2f72`](https://testnet.snowtrace.io/tx/0x26429301e27abd25e797941252abf168c327d6cd306a40828a8c17d11ec42f72) |
| Orchestrator recalls the worker's unspent 900 (not a spend) | `AgentAccounts.recall` | 45,133 | [`0xe692…b74f`](https://testnet.snowtrace.io/tx/0xe69219fced6afcb9d15357adffbc0ccb9f8af63360cb3934e16c3e38a18fb74f) |

Final state read back from the contracts: root spent-in-window 400 (its own 300 pool deposit plus the worker's 100 channel, counted up the tree), worker spent-in-window 100, provider balance 9, worker balance 0, root balance 9,600. These match the Python reference's scenario E exactly.

Three-member pool settlement was measured on a local chain at 191,841 gas (about 64,000 per member); the Fuji run settled one channel.

## The cost of a session

A Foliant session costs a fixed number of transactions however many calls it contains:

| Route | On-chain work per session | Gas per session |
| --- | --- | ---: |
| Channel | open + settle (close is optional and returns the remainder) | 524,450 |
| Pool | join + the member's share of one batch settlement | ≈ 335,000 |
| Per-call x402 `exact` (for comparison) | one authorised USDC transfer per call, submitted by the facilitator | ≈ 75,000 × calls |

The `exact` figure is an estimate for an EIP-3009 `transferWithAuthorization` on Avalanche (a USDC transfer plus signature verification); it is not measured here and should be replaced with a measured value when a facilitator run is available.

| Calls in the session | Channel route | Pool route | Per-call `exact` | Foliant saving |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 524k | 335k | 375k | pool 11 %; channel costs more |
| 10 | 524k | 335k | 750k | 30–55 % |
| 100 | 524k | 335k | 7.5M | 93–96 % |
| 1,000 | 524k | 335k | 75M | > 99 % |

Break-even against per-call payment is about 5 calls on the pool route and 7 on the channel route. Above that, cost is flat and latency per call is the provider's HTTP round-trip alone: no transaction, no confirmation, no facilitator in the path.

Account set-up (register, deposit, delegate) is a one-off, not per session; a crew is registered once and its workers keep their accounts across sessions. Recalling a worker's unspent funding is 45k gas and, like delegation, is not a spend against any policy.

## In money

Fuji's gas price during the run was negligible (0.16 nAVAX; the whole run cost under 0.000001 AVAX). At Avalanche mainnet's usual base fee of about 1 nAVAX and an AVAX price of about $11, one gas unit is roughly $1.1 × 10⁻⁸:

- a 100-call session: Foliant channel ≈ $0.006, pool ≈ $0.004, per-call `exact` ≈ $0.08;
- a 1,000-call session: Foliant ≈ $0.004–0.006, per-call ≈ $0.83.

Avalanche is already cheap, so the money saved per session is small in absolute terms; the operational difference is that a crew's spend is bounded by the ledger and settles once, and that per-call latency has no chain in it. On chains with higher fees the same gas figures translate into proportionally larger savings.

## What this run did not measure

A pool settlement on Fuji with several members (measured locally only); the facilitator-side cost of `exact` (estimated); and the http flow through `demo/serve_chain.py` on Fuji, which was exercised on a local chain in `tests/test_chain.py` (ten paid calls, one join transaction, one settlement transaction) and is unchanged by the network.
