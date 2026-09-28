# Foliant Whitepaper v0.1

28 September 2026 · Gareth Oyston, Machine Quotient Ltd

Draft for review. Machine Quotient Ltd.

## Abstract

Foliant is a three-layer blockchain network designed for machine-to-machine and human-to-machine value transfer. A wide, slow settlement layer provides neutral finality and data availability; a fast execution core with an object-centric Move virtual machine and DAG-based Byzantine fault-tolerant consensus provides \~1 s finality and parallel execution; a marketplace of sovereign app-chains, connected by light-client interoperability, provides specialisation without fragmenting security. Foliant's native contribution is the **agent account**: an on-chain account object whose keys can live inside attested trusted execution environments, whose spending is bounded by on-chain policy, and which can open streaming payment channels that settle HTTP-level metered requests at sub-cent cost. Governance is track-based and on-chain, with a treasury funded from fees rather than inflation, and a hard-capped native token, FOL. The design borrows deliberately from Bitcoin, Ethereum, Cardano, Polkadot, Solana, Avalanche, Cosmos, Sui, Aptos, Near and Hyperliquid, and states for each borrowed component what was given up to obtain it.

## 1. Motivation

Software agents are becoming economic actors, and no existing network was designed with them as first-class users. An agent that calls an API ten thousand times an hour needs an account it can hold without a human key ceremony, a spending limit its operator can enforce cryptographically rather than contractually, and a way to pay a fraction of a cent per call with finality measured in the time of a network round trip. Card rails cannot do this; the agent-payment protocols emerging in 2025-26 (HTTP 402 metering, agent payment protocols from exchanges and card networks) run on general-purpose chains that treat an agent as an ordinary externally owned account.

The chains of the last decade each solved one part of the problem. Bitcoin proved neutral settlement; Ethereum proved programmable money and a rollup-centric scaling path; Solana, Sui and Aptos proved that parallel execution and sub-second BFT finality are practical; Cosmos proved trust-minimised interoperability through light clients; Avalanche proved that sovereign chains can be cheap to launch; Cardano and Polkadot proved that a protocol can govern and fund itself on-chain. None combined them, because several of these properties are in tension inside a single layer. Foliant's premise is that they can be combined across layers, and that the agent economy is the workload that justifies doing so.

Three observations shape the design:

1. **Capacity is no longer scarce.** Chains with six-figure theoretical throughput run at a few hundred transactions per second of real demand. A new network must win on a workload, not on a benchmark.
2. **Security and speed live in different validator sets.** Sub-second finality needs a small, well-provisioned validator set; credible neutrality needs a large, cheap one. A network that wants both must checkpoint the fast set into the slow one.
3. **Interoperability is a standard, not a feature.** Multisig bridges have lost more user funds than any other component. Light-client verification (IBC) is the only bridge design with a security argument, and it now reaches chains without native light clients through attestation.

## 2. Design principles

1. **One property per layer.** Each layer optimises for exactly one of neutrality, speed or sovereignty, and the layers are coupled by verifiable commitments rather than shared state.
2. **Borrow production components; invent only the agent layer.** Consensus, VM, data availability and interoperability are taken from open-source systems with years of mainnet operation. Novel code is confined to agent accounts, payment channels and their integration.
3. **No single client.** The execution core launches with two independent client implementations; a bug in one cannot halt the network.
4. **Light clients or nothing.** All cross-chain messages are verified by light clients (or attested light clients where the counterparty chain has none). There are no multisig bridges.
5. **Deterministic cost before submission.** Transactions declare the objects they touch, so fees and outcomes are known before broadcast, as in extended-UTXO systems. A transaction whose declared set is complete skips optimistic execution entirely.
6. **Fees fund the protocol; inflation does not.** Treasury income is a fixed fraction of fees. Issuance is a decaying staking subsidy that reaches zero.
7. **Governance is on-chain, bounded and slow.** Parameter changes and upgrades pass through tracks with delays proportional to their blast radius; a constitutional committee can veto but not propose.
8. **Formally verify what cannot be patched.** Settlement consensus and the token contract are formally verified; everything else is audited and upgradable.
9. **Post-quantum at genesis.** Every signature scheme in the protocol is versioned and replaceable by runtime upgrade, and the keys that cannot be rotated quickly (settlement validator keys, account recovery keys) use a hash-based post-quantum scheme from the first block. No live chain has yet migrated its signatures; Foliant avoids having to.
10. **Speak the standards agents already use.** The agent layer exposes HTTP 402 (x402) and the Machine Payments Protocol rather than a Foliant-only format, so an agent built for Coinbase's or Stripe's rails pays on Foliant without a new client.

## 3. Architecture overview

Foliant consists of a settlement layer (L1), an execution core (L2) and an app-chain marketplace (L3), with one token and one governance system spanning all three.

```mermaid
flowchart TB
  subgraph L3["Layer 3 · App-chain marketplace (sovereign, cheap to register)"]
    direction LR
    A1["Agent chain<br/>AI payments, TEEs"]:::accent
    A2["DeFi chain<br/>CLOB precompile"]
    A3["Enterprise chain<br/>PoA, permissioned"]
    A4["EVM chain<br/>Solidity tooling"]
  end
  subgraph L2["Layer 2 · Execution core (fast, ~100-300 validators)"]
    direction LR
    B1["Move object VM<br/>parallel, Block-STM style"]
    B2["DAG BFT consensus<br/>Mysticeti-style, ~1 s finality"]
    B3["Fee markets, 2 clients<br/>local fees, client diversity"]
  end
  subgraph L1["Layer 1 · Settlement and data availability (wide, slow, neutral)"]
    direction LR
    C1["Wide validator set<br/>home hardware, ~1-2 min finality"]
    C2["Blob data availability<br/>PeerDAS-style sampling"]
    C3["Settlement roots<br/>hard-capped token supply"]
  end
  G["Across all layers: OpenGov tracks, delegated reps, fee-funded treasury, forkless upgrades"]
  L3 <-- "IBC light clients · Warp-style messaging · opt-in shared security" --> L2
  L2 -- "state roots and blobs checkpointed every few seconds" --> L1
  L1 ~~~ G
  classDef accent fill:#dbeafe,stroke:#2563eb,stroke-width:2px;
```

A transaction on the execution core is final in about one second under the core's own BFT consensus. Every few seconds the core's validators post a state root and the block data as blobs to the settlement layer; once the settlement layer finalises that checkpoint (1-2 minutes), the core's history up to that point is irreversible even if the core's validator set is later compromised. App-chains relate to the core the same way the core relates to settlement: they may post checkpoints to the core (and so inherit its finality) or run fully sovereign and connect by IBC only.

Users and agents interact almost entirely with the core and app-chains. The settlement layer holds the canonical FOL ledger, staking records and checkpoints; it exposes a minimal transaction set (transfer, stake, checkpoint, governance vote) and no general-purpose smart contracts.

## 4. Settlement layer

The settlement layer is optimised for the number and independence of its validators, not for speed. Its design target is thousands of validators on consumer hardware with residential bandwidth.

**Validators and staking.** Proof-of-stake with a low minimum bond (target: an amount a hobbyist can reach) and a maximum effective balance per validator to force large stakers to run many keys, as Ethereum does. Nominated staking (Polkadot NPoS) lets token holders back validators without running hardware; the election algorithm maximises the stake behind the least-backed elected validator, which flattens stake concentration.

**Consensus.** A slot-and-epoch protocol in the Ouroboros/Gasper family: a random leader proposes each slot's block, validators attest, and a checkpoint is finalised when two-thirds of stake has attested to it across two epochs. Slot time 6 s, epoch 32 slots, finality \~1-2 min. This protocol is the one component to be formally verified, because its safety cannot be patched after the fact. Slashing applies to double-proposal and surround-voting; inactivity leak reduces the stake of validators who stop attesting during a non-finalising period so that finality resumes.

**Data availability.** Blocks carry blobs: the execution core and checkpointing app-chains post their transaction data as erasure-coded blobs, and validators verify availability by sampling (PeerDAS-style) rather than downloading everything. Blobs are pruned after a retention window (18 days initially); their commitments remain forever. This gives the fast layers Ethereum-grade data availability without requiring settlement validators to execute the fast layers' transactions.

**Transaction set.** Deliberately minimal: FOL transfer, stake and unstake, nominate, submit checkpoint, submit blob, governance vote and referendum. No general smart contracts. This keeps the client small, the attack surface narrow and the hardware bar low.

**Checkpoints.** A checkpoint from the execution core is a signed state root plus blob references from at least two-thirds of core stake. The settlement layer verifies the signature set against the core validator registry it holds, and stores the root. A checkpoint that conflicts with an earlier finalised one is rejected and the signing core validators are slashed on the settlement layer, where their stake is held.

## 5. Execution core

The execution core is where applications and agents live. It adopts the object model and consensus of Sui and the optimistic parallel scheduler of Aptos, with Solana's local fee markets.

**Object model.** State consists of objects, each with an owner and a version. Owned objects can be mutated only by their owner; shared objects can be mutated by anyone under the rules of their defining Move module. A transaction declares the objects it reads and writes. Transactions touching only owned objects are causally independent of everything else and take the *fast path*: they are certified by a two-thirds quorum of validators without ordering through consensus, and finalise in a few hundred milliseconds. Transactions touching shared objects are ordered by consensus.

**Virtual machine.** Move, in its object-centric dialect. Move's linear resource types make assets un-duplicable and un-droppable at the language level, which matters when agents hold funds. Modules are upgradable under an on-chain policy set at publish time (immutable, owner-upgradable or governance-upgradable).

**Parallel execution.** Consensus-ordered transactions are executed with a Block-STM scheduler: all transactions in a block run optimistically in parallel, conflicts are detected from the declared read/write sets, and conflicting transactions re-execute in order. Declared sets bound the worst case and make fees predictable.

**Consensus.** A DAG-based BFT protocol in the Mysticeti family: every validator proposes blocks each round, blocks reference earlier blocks, and commitment is decided by the structure of the DAG rather than by explicit voting messages. Three message delays to commit; \~0.5-1 s finality in practice. Validator count 100-300, weighted by FOL stake delegated on the settlement layer.

**Fee markets.** Fees have a base component (burned in part, sent to treasury in part) that adjusts per block to target 50% utilisation, plus a *local* component per contended shared object, so a popular auction raises the price of touching that object without raising the price of an unrelated transfer. Fees are denominated in FOL, but a transaction may name a **paymaster**: a registered contract that accepts a whitelisted stablecoin from the sender and pays the FOL fee itself, so payments users never hold the volatile asset (the model Circle's Arc uses with USDC as gas). The paymaster buys and burns FOL, so the fee split in §9 is unchanged. A **zero-fee lane** carries settlement of whitelisted assets (initially the network's reference stablecoins and channel settlements) at no cost to the sender, funded from the treasury and capped per account per hour; transactions over the cap fall back to the normal fee market. This follows Plasma's subsidised transfer path and exists so that an agent's first payment costs nothing to set up.

**Two clients.** The core ships with two independently implemented validator clients (working assumption: one derived from the Sui reference implementation in Rust, one written from the specification in Go or Zig). No client may exceed two-thirds of stake; delegation programmes and rewards are weighted to enforce this.

**Order-book precompile.** A native central limit order book, in the manner of Hyperliquid's HyperCore, is exposed as a shared object with a native matching engine. Move contracts read and place orders on it directly, removing the oracle hop that on-chain derivatives otherwise need. Orders are submitted **sealed**: encrypted to a threshold key held by the current validator committee and decrypted only after the block's order set is fixed, then matched in a batch per small block (the Penumbra pattern), so no party can see or front-run an order before it is committed. Any value the matching engine captures from price improvement in a batch is **rebated to the resting liquidity** on that book rather than to the proposer, as Unichain's verifiable block building does for its liquidity providers.

**Dual block cadence.** Small blocks every \~0.5 s carry transfers, channel updates and order-book operations; large blocks every \~30 s carry heavy computation such as proof verification, so that verifying a zkML proof does not delay a payment.

## 6. Agent accounts and payment channels

This section is Foliant's novel contribution. An agent account is a Move object on the execution core that binds three things: a key that may live inside an attested enclave, a spending policy enforced by the chain, and a set of payment channels that let the agent pay per request without touching the chain for every call.

**6.1 Account object.** `AgentAccount { owner: address, signer: KeyRef, policy: PolicyRef, attestation: Option<Attestation>, channels: vector<ChannelRef>, nonce: u64 }`. The `owner` is the principal (a person or organisation) who created the agent and can rotate or revoke it. The `signer` is the key the agent uses day to day. The account is an owned object of the owner but is *operated* by the signer: transactions signed by the signer are valid only if they pass the policy check.

**6.2 Attestation.** The signer key may be generated inside a trusted execution environment (Intel TDX, AMD SEV-SNP, AWS Nitro). The enclave produces a remote attestation quote binding the key to a measured code image; the quote is verified by a native precompile and stored in the account. Counterparties can then require that payments come from a key that provably lives inside known agent code. Attestation is optional: an account without one is simply a policy-bounded hot wallet. TEE trust is hardware trust, so attestation is a signal, not a guarantee; policy limits bound the loss if an enclave is broken.

**6.3 Spending policy.** `Policy { per_tx_max, per_window_max, window_secs, allow_list: Option<vector<address>>, deny_list, expiry, escalation: Option<address> }`. The policy is checked by the runtime before execution, not by a contract, so it cannot be bypassed by calling a different module. Transactions over the limit fail unless co-signed by the `escalation` key (typically the owner). Policies are objects, so an organisation can share one policy across many agents and change it in one transaction.

**6.4 Payment channels.** A channel is a shared object `Channel { payer: AgentAccount, payee: address, deposit: Coin, balance_to_payee: u64, seq: u64, timeout }`. Opening deposits funds; each off-chain update is a signed tuple `(channel_id, seq, balance_to_payee)` where the balance only increases. The payee may settle any time by submitting the latest update; the payer may close by submitting its latest update and waiting `timeout` for the payee to submit a higher one. Channel updates are signed under the agent's policy: the enclave refuses to sign an update that would breach `per_window_max`. Settling a channel is an owned-object transaction on the payee's side and takes the fast path.

**6.5 Metering flow.** The flow follows the HTTP 402 convention used by x402 and similar protocols, so any HTTP API can be metered with a middleware and no chain-specific client.

```mermaid
flowchart LR
  S1["1. Agent calls API<br/>HTTP request"] --> S2["2. API answers 402<br/>price, channel id"]
  S2 --> S3["3. Agent signs update<br/>inside TEE, within policy"]:::accent
  S3 --> S4["4. API verifies, serves<br/>no chain round trip"]
  S4 -- "next call reuses the open channel" --> S1
  S4 --> S5["5. API settles batch<br/>latest update on-chain"]
  S3 -- "policy enforced" --> P["Policy object on core<br/>limits, allow-list, expiry"]
  classDef accent fill:#dbeafe,stroke:#2563eb,stroke-width:2px;
```

Steps 1-4 involve no chain interaction and complete within the latency of the API call itself. Step 5 happens whenever the API chooses (every N calls, every hour, or when the channel nears its deposit). One on-chain transaction settles thousands of calls, so the per-call cost is the amortised fee divided by N, far below a cent.

**6.6 Streaming.** For continuous services (inference tokens, compute-seconds, data feeds) a channel may carry a rate rather than discrete updates: `(channel_id, rate_per_sec, start_ts)` signed once; the payee's claimable balance is `rate × (now − start)` until either side sends a stop. This is the Sablier/Superfluid pattern moved into the channel layer so the stream itself never touches the chain.

**6.7 Discovery and reputation.** Agents and APIs register service descriptors (price, unit, attestation requirements) as objects; a payee can require a minimum attested code hash, a payer can require a payee's settlement history. Reputation is the set of settled channels, which is public and unforgeable.

**6.8 Interaction with verifiable inference.** A channel update may carry a commitment to the request and response; a payee that later submits a zkML or opML proof against that commitment can claim a bonus or defend against a dispute. Foliant does not mandate proofs; it makes them attachable.

**6.9 Wire compatibility.** The Agent chain implements two external standards as first-class request formats: the HTTP 402 flow of x402, and the Machine Payments Protocol that Tempo launched with Stripe in March 2026. A payee's 402 response may carry Foliant channel terms alongside x402 or MPP terms, and the Foliant middleware accepts any of them. This makes Foliant a settlement option inside the rails that Coinbase and Stripe are standardising rather than a competing rail.

**6.10 Pooled channels.** A dedicated channel per agent-payee pair does not scale to one API serving thousands of agents. Foliant adds a **pool**: a shared object funded by many payers, coordinated by the payee (or a third-party operator), in which each payer holds an individually signed, unilaterally exitable claim, following Bitcoin's Ark construction. A payer joins a pool with one on-chain transaction, pays inside it off-chain exactly as in a channel, and can exit at any time by submitting its latest claim and waiting the timeout; the coordinator settles the pool periodically with one transaction. If the coordinator disappears, every payer's exit path still works. Pools reduce per-agent on-chain setup from one transaction per counterparty to one per pool.

**6.11 Compute and data market objects.** The chains built for AI workloads in 2025-26 (Ritual, 0G, Sahara, Story) converge on three primitives that Foliant standardises as object types on the Agent chain rather than as its own applications: a `ServiceOffer` (price, unit, attestation requirements, model or dataset identifier), a `Receipt` (a channel update that commits to request and response hashes, optionally with a zkML or opML proof reference), and a `Contribution` (a data or model contribution with a licence and a royalty split that cascades to derivatives, in the manner of Story's IP graph). Marketplaces, verifiers and licensing services are then applications over shared objects, and any of them can be replaced without changing the protocol.

## 7. App-chain marketplace and interoperability

App-chains give Foliant specialisation without forcing every application onto the core's validator set or VM. The marketplace follows Avalanche9000's economics and Cosmos's connectivity, with Polkadot's shared security as an option.

**Registration.** Any chain registers on the settlement layer by paying a continuous fee in FOL (order of a few FOL per month, set by governance) and publishing its genesis, validator set and light-client type. There is no slot auction and no per-validator bond on the settlement layer. Registration gives the chain a name in the network registry and the right to open IBC connections to the core and to other registered chains.

**Security models.** A registered chain chooses one of three:

- *Sovereign*: its own validators and stake; Foliant verifies it by light client only. Cheapest, least secure.
- *Checkpointed*: its own validators, but state roots are posted to the core and become irreversible once the core checkpoints to settlement. Protects against long-range attacks on the app-chain.
- *Shared security*: validators are drawn from core stake that has opted in, and are slashed on the settlement layer for misbehaviour on the app-chain. The app-chain pays rent in FOL to the validators who secure it, priced by a continuous auction as in Polkadot's coretime market.

**Virtual machines.** App-chains choose their execution environment: Move (sharing tooling with the core), EVM (for Solidity teams and existing contracts), CosmWasm, or a custom VM. An EVM app-chain with shared security is the expected path for most DeFi teams migrating from Ethereum L2s.

**Interoperability.** All cross-chain messaging is IBC. Between Foliant chains, light clients verify counterparty consensus directly. To chains without cheap light clients (Ethereum L1, Solana, Bitcoin), Foliant uses attested light clients: a set of attestors runs full nodes inside TEEs and signs headers, and the attestation quotes are verified on-chain. This is weaker than a native light client and stronger than a multisig; attestors are bonded in FOL and slashed for signing conflicting headers. Token transfers use mint-and-burn (IBC IFT) rather than wrapped vouchers, so an asset has one canonical form across the network. General message passing (IBC GMP) lets a contract on one chain call a contract on another with a delivery proof.

**Warp-style fast messaging.** Between chains that share security, messages are additionally relayed by the shared validator set with BLS-aggregated signatures, giving sub-second cross-chain calls without waiting for light-client header verification. The IBC path remains the fallback and the source of truth.

## 8. Governance

Foliant is governed on-chain by FOL holders through tracks whose delay and threshold scale with the blast radius of the decision. The design follows Polkadot OpenGov and Cardano CIP-1694.

**Tracks.** Every referendum is filed on a track. Initial tracks: *treasury small* (≤ 0.1% of treasury, 3-day vote), *treasury large*, *parameter change* (fee targets, registration fee, blob retention), *runtime upgrade* (28-day enactment delay), *emergency* (validator-set or attestor removal, 24-hour vote, requires committee co-sign) and *constitutional* (changes to tracks, thresholds or the committee). Tracks run concurrently; a track may cap its concurrent referenda.

**Voting.** Stake-weighted with conviction: a voter may lock FOL for longer to multiply their weight (up to 6× for a 32-week lock). Approval and turnout thresholds decay over the voting period so that a proposal with broad support passes quickly and a contentious one needs time.

**Delegated representatives.** Holders may delegate per track to a registered representative (DRep). Representatives publish a statement and a voting record; delegation is revocable at any block. This gives Cardano-style participation without requiring every holder to read every proposal.

**Constitutional committee.** A committee of 7-15 elected members holds a veto over runtime upgrades and constitutional changes that violate the written constitution, and a co-sign on the emergency track. It cannot propose. Members serve fixed terms and are elected by FOL holders.

**Forkless upgrades.** The execution core and settlement layer runtimes are deployable as WASM modules approved by referendum; clients load the new runtime at the enactment block. Client software still needs updating for changes below the runtime (networking, storage), which is why two clients and a long enactment delay matter.

**Treasury.** The treasury is a settlement-layer account funded by a fixed share of fees from all three layers (see §9). It spends only through the treasury tracks. Recurring programmes (validator delegation, audits, grants) are approved as bounties with elected curators, so that individual disbursements do not each need a referendum.

## 9. Token economics

FOL is the single staking, fee and governance asset of the network. Supply is hard-capped; staking rewards come from a decaying issuance that reaches zero, after which validators are paid from fees alone. All figures in this section are proposals for the legal and economic review, not commitments.

**Supply.** Maximum supply 1,000,000,000 FOL, fixed in the settlement-layer runtime and changeable only through the constitutional track.

**Issuance.** Staking rewards are issued per epoch on a geometric decay with a half-life of 4 years, from an initial rate equivalent to 4% of genesis supply per year, until cumulative issuance reaches the cap. Cumulative issuance after t years:

```latex
I(t) = I_0 \, \frac{1 - 2^{-t/4}}{\ln 2 / 4}
```

where I₀ is the initial annual issuance. This converges to about 5.8 × I₀, so with I₀ = 4% of genesis supply, total issuance is bounded at roughly 23% of genesis supply, and the cap is set so that genesis allocation plus issuance equals 1,000,000,000.

**Fee split.** Every fee on the settlement layer, the core and shared-security app-chains is split: 50% burned, 30% to the block proposer and attesting validators, 20% to the treasury. Fees paid through a stablecoin paymaster are converted to FOL by the paymaster before the split, so the burn is preserved whatever the sender paid in. The zero-fee lane is paid for by the treasury at the prevailing base fee, with a governance-set annual budget; when the budget is exhausted the lane closes until the next period. Burning offsets issuance; at sustained utilisation the network is net deflationary. Sovereign app-chains keep their own fees but pay registration in FOL, which follows the same split.

**Staking.** Settlement validators bond FOL on the settlement layer; core validators bond FOL on the settlement layer as well, registered to the core. Nominators delegate to either. Unbonding takes 21 days. Slashing: 0.1% for downtime beyond a threshold, 5% for equivocation, up to 100% for a coordinated attack detected by the fraction of stake involved (Ethereum's correlation penalty).

**App-chain rent.** Registration fee and shared-security rent are paid in FOL, streamed per block from the app-chain's registry account. Rent is priced by a continuous auction against available core stake; governance sets floor and ceiling.

**Genesis allocation (proposal).**

| Bucket | Share | Vesting |
| --- | --- | --- |
| Community: airdrop to Agent-chain users, testnet participants, delegation programme | 30% | Airdrop at core mainnet; programmes over 4 years |
| Treasury (governance-controlled) | 25% | Unlocked, spendable only by referendum |
| Core contributors and early team | 18% | 1-year cliff, 4-year linear |
| Investors (pre-seed to Series A) | 17% | 1-year cliff, 3-year linear |
| Foundation operations and validator bootstrap | 10% | 4-year linear |

No public sale is assumed. Whether a token exists before the core mainnet, and in which jurisdictions it may be distributed, is a legal decision addressed in the roadmap.

## 10. Security model and threat analysis

Foliant's security argument is layered: the fast layers may be compromised transiently, but nothing finalised by the settlement layer can be reverted without corrupting more than one-third of settlement stake, and every cross-layer and cross-chain claim is verified by a light client or a bonded attestation.

**Assumptions.** Settlement: fewer than one-third of bonded stake is Byzantine, and the network is partially synchronous. Core: fewer than one-third of core stake is Byzantine for liveness and safety of un-checkpointed blocks; once checkpointed, safety rests on settlement. TEEs: attestation proves code identity but not freedom from side channels; policies bound the loss from an enclave break.

| Threat | Layer | Defence |
| --- | --- | --- |
| Core validators collude to fork after checkpoint | Core / settlement | Checkpoint conflict is detected on settlement; colluding stake is slashed there; the earlier checkpoint stands |
| Core halts (client bug, one-third offline) | Core | Second client; users can force-include transactions via the settlement layer after a timeout; inactivity leak on the core validator set |
| Settlement long-range attack | Settlement | Weak subjectivity checkpoints distributed with clients; 21-day unbonding makes old keys worthless after that window |
| Data withholding by core proposers | Settlement DA | Blobs must pass availability sampling before a checkpoint is accepted; a checkpoint whose blobs are unavailable is invalid |
| Attestor bridge signs a false header | Interop | Attestors bonded in FOL and slashed for conflicting signatures; TEE attestation ties the signing key to audited code; rate limits cap per-hour outflow |
| Agent key compromised | Agent layer | Policy limits are runtime-enforced; the owner revokes the signer; channels close with timeout so a thief cannot drain deposits beyond the policy window |
| Enclave broken (side channel) | Agent layer | Same as key compromise; attestation is advisory, not load-bearing for funds |
| Channel counterparty submits stale state | Agent layer | Monotonic sequence numbers; the honest party submits the higher-sequence update during the timeout |
| Governance capture | Governance | Conviction locking, track delays proportional to impact, committee veto on constitutional changes, treasury spend caps per track |
| Client monoculture | Core | Delegation programme and rewards weighted so no client exceeds two-thirds of stake |
| MEV extraction against agents | Core | Fast-path transactions are not orderable; shared-object transactions use encrypted mempool submission to the proposer (threshold decryption after ordering) |

Three further threats come with the additions in §5 and §6. *Zero-fee lane spam*: bounded by per-account hourly caps and the treasury budget; an account that hits its cap pays normal fees. *Pool coordinator failure or fraud*: every claim is unilaterally exitable with the payer's own signature, so a coordinator can delay settlement but cannot take funds; coordinators are bonded and lose the bond for submitting a stale pool state. *Quantum adversary*: settlement validator and recovery keys are hash-based from genesis; hot keys (agent signers, core validators) use fast classical schemes and are rotated to post-quantum schemes by runtime upgrade under the signature-agility principle before large-scale quantum attacks are practical.

**What is formally verified.** The settlement consensus protocol (safety and plausible liveness), the FOL token module (supply invariants) and the channel settlement logic (no party can claim more than the last co-signed balance). Everything else is audited and upgradable.

**Bug bounty.** A standing programme funded from treasury, with rewards scaled to the layer affected; settlement-layer critical findings pay the most.

## 11. Comparison with existing networks

Foliant matches the fast monolithic chains on execution and the modular ecosystems on interoperability, and adds an agent layer none of them has. What it gives up is simplicity: three layers are harder to build, explain and secure than one.

| Property | Foliant | Ethereum + L2s | Solana | Sui / Aptos | Cosmos | Polkadot | Avalanche |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Finality (user-facing) | \~0.5-1 s core; 1-2 min settlement | 12.8 min L1; L2 soft-confirm instantly | 100-150 ms (Alpenglow) | sub-second | instant (single slot) | \~30 s | \~2 s |
| Parallel execution | yes (object model + Block-STM) | no at L1 | yes | yes | per-chain (BlockSTM arriving) | per-parachain | per-L1 |
| Validator count (fast layer) | 100-300 core; thousands settlement | 1.36M L1 | \~675 | \~86-130 | per-chain | \~600 | per-L1 |
| Independent clients at launch | 2 | 5+ | 2 (Agave/Jito, Firedancer) | 1 | 1 | 1 | 1 |
| Interop standard | IBC + attested light clients | bridges, native L2 bridges | Wormhole | Wormhole | IBC | XCM | Warp |
| App-chain cost | monthly fee | RaaS subscription | n/a | n/a | run own validators | coretime auction | 1-10 AVAX/month |
| Shared security option | yes | yes (rollups) | n/a | n/a | opt-in (ICS) | yes | no |
| On-chain governance and treasury | yes, fee-funded | no | no | partial | per-chain | yes, inflation-funded | no |
| Agent-native accounts and channels | yes | via contracts | via contracts | via contracts | no | no | no |
| Order-book precompile | yes | no | no (app-level) | no | no | no | no |
| Data availability for fast layers | native blobs with sampling | native blobs (PeerDAS) | n/a | n/a | none | relay chain | none |

Relative to Hyperliquid, Foliant trades the 21-validator, 70 ms design for a wider set and a slower but neutral settlement root. Relative to Near, Foliant defers sharding: the object model and app-chains carry the load until dynamic resharding of the core is needed, which the settlement layer's data availability is designed to accommodate.

## 12. Roadmap

The agent layer ships first as a standalone chain; the composite core follows once the agent layer has fee revenue and funding. Each phase ends at a gate that is a condition, not a date.

1. **Design and seed (months 0-4).** This whitepaper; agent-account, channel and pool specification; devnet of the agent modules on a Cosmos SDK chain; HTTP 402 and MPP middleware and an SDK for one language; one design partner signed. Gate: seed funding closed.
2. **Agent chain (months 4-15).** Public testnet with external validators; zero-fee lane and stablecoin paymaster; compute-and-data market objects; two audit rounds on the agent modules and IBC configuration; IBC connection to at least one major ecosystem via an attested light client; mainnet with fees in stablecoins. Gate: audits passed, mainnet live.
3. **Traction and raise (months 15-24).** Paying agents and APIs on mainnet; delegation programme; MPC custody as a second key model; MiCA white paper and UK financial-promotion review if a token is to be distributed; Series A. Gate: Series A closed.
4. **Foliant core and settlement (months 24-42).** Two core clients; settlement layer with formally verified consensus and hash-based validator keys; order-book precompile with sealed orders; Agent chain migrates to become the first app-chain under shared security; app-chain registry opens; FOL genesis and airdrop. Gate: two clients above one-third stake each, audits and verification complete, mainnet.
5. **After mainnet.** Encrypted mempool for all shared-object transactions, shared sequencer service for app-chains, recursive settlement proofs for bridges, optional shielded execution, dynamic resharding of the core as demand requires.

## References

Design sources for the components Foliant adopts, all opened 28 September 2026.

- Ethereum roadmap (Pectra, Fusaka, PeerDAS, Glamsterdam): [ethereum.org/roadmap](https://ethereum.org/roadmap/)
- Solana Alpenglow consensus and Firedancer client: [BlockEden, Feb 2026](https://blockeden.xyz/blog/2026/02/26/solana-firedancer-alpenglow-1m-tps/)
- Polkadot JAM and OpenGov: [JAM transition, Sep 2026](https://www.hokanews.com/2026/09/polkadot-prepares-major-jam-transition.html)
- Cardano Leios and Voltaire governance: [cardano.org, May 2026](https://cardano.org/news/2026-05-14-cardano-is-ready-to-grow/)
- Avalanche9000 L1 registration model: [Avalanche support](https://support.avax.network/en/articles/9868736-what-is-avalanche9000); [Etna motivation](https://build.avax.network/blog/etna-upgrade-motivation)
- Cosmos IBC, Attestor light clients, IFT, GMP, BlockSTM: [Cosmos Stack roadmap 2026](https://cosmos.network/blog/the-cosmos-stack-roadmap-2026)
- Sui object model and Mysticeti v2: [sui.io](https://www.sui.io/blog/mysticeti-v2-sui-consensus)
- Aptos Block-STM and AptosBFT: [Chainspect Aptos](https://chainspect.app/chain/aptos)
- Near Nightshade dynamic resharding: [Chainspect Near](https://chainspect.app/chain/near)
- Hyperliquid HyperCore/HyperEVM dual architecture: [CleanSky, 2026](https://cleansky.io/blog/hyperliquid-architecture-hypercore-hyperevm-2026/)
- Bitcoin Lightning capacity trend: [Spark, 2026](https://www.spark.money/research/lightning-network-2026-state)
- HTTP 402 agent payments: [Coinbase x402](https://www.coinbase.com/developer-platform/products/x402); [CoinDesk, Mar 2026](https://www.coindesk.com/markets/2026/03/11/coinbase-backed-ai-payments-protocol-wants-to-fix-micropayment-but-demand-is-just-not-there-yet)
- Regulatory context: [FCA cryptoasset regime](https://www.fca.org.uk/firms/new-regime-cryptoasset-regulation); [Skadden, Jul 2026](https://www.skadden.com/insights/publications/2026/07/fca-finalises-core-rules-for-the-uk-cryptoasset-regime); [MiCA status, Apr 2026](https://binar.com/insights/mica-april-2026-eu-crypto-market/); [CLARITY Act status](https://www.kucoin.com/blog/en-2026-clarity-act-the-lastest-status-and-update)

Sources for the additions of 28 September 2026:

- Tempo Machine Payments Protocol: [CoinDesk, Mar 2026](https://www.coindesk.com/tech/2026/03/18/stripe-led-payments-blockchain-tempo-goes-live-with-protocol-for-ai-agents)
- Arc, USDC as gas, mainnet 16 Sep 2026: [arc.io](https://www.arc.io/blog/arc-mainnet-goes-live-on-september-16-2026)
- Plasma zero-fee stablecoin transfers: [plasma.org](https://www.plasma.org/company/blog/plasma-mainnet-beta-and-xpl)
- Ethereum Lean consensus, hash-based signatures: [HackMD, 2026 plan](https://hackmd.io/@tcoratger/ryS1ElrWbx); Naoris post-quantum L1: [The Quantum Insider, Apr 2026](https://thequantuminsider.com/2026/04/01/naoris-protocol-launches-mainnet-introducing-post-quantum-layer-1-blockchain/)
- Ika 2PC-MPC custody: [sui.io](https://www.sui.io/blog/ika-dwallet-mpc-network-interoperability)
- Fuel declared access lists: [Fuel docs](https://docs.fuel.network/docs/fuel-book/the-architecture/the-fuelvm/)
- Namada shielded pool: [namada.net](https://namada.net/blog/namada-mainnet-is-live); Zama FHE: [docs.zama.org](https://docs.zama.org/protocol/protocol/overview); Aztec: [Road to Mainnet](https://aztec.network/blog/road-to-mainnet)
- Linera microchains: [linera.dev](https://linera.dev/protocol/overview.html); Sei Giga: [Everstake](https://everstake.com/resources/blog/sei-giga-upgrade-50x-faster-execution-for-the-evm-era); MegaETH: [megaeth.com](https://www.megaeth.com/); Monad: [monad.xyz](https://www.monad.xyz/announcements/parallel-execution-monad)
- Ark, Unichain, Penumbra, Ritual, 0G, Sahara and Story were reviewed from secondary coverage; primary documentation to be confirmed in the specification phase.

The companion study with feasibility and costs: Composite Blockchain Study — Design, Feasibility & Costs (28 Sep 2026).
