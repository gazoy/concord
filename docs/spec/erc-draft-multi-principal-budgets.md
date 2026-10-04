---
eip: <unassigned>
title: Multi-Principal Agent Budgets
description: A spend budget pooled from several funders, where every spend is attributable to one funder's capacity and bound by that funder's policy
author: Gareth Oyston (@gazoy)
discussions-to: <ethereum-magicians thread, to be opened before this is submitted>
status: Draft
type: Standards Track
category: ERC
created: 2026-10-04
requires: 20, 165, 712, 1271, 7528
---

## Abstract

This specification defines a budget that more than one principal funds and that one agent, or a tree
of agents, spends. A funder makes a *contribution*: an amount of one asset, held exclusively for that
funder, carrying that funder's own spending policy and its own revocation epoch. A *pool* is a set of
contributions spendable by the same account. Every spend names exactly one contribution, is checked
against that contribution's policy and against every ancestor policy in the delegation tree before any
value moves, and is permanently attributable to the contribution it drew. One funder's exhaustion or
revocation leaves every other funder's capacity spendable.

The exclusivity is the mechanism, not an implementation detail. It is what makes attribution
decidable, what keeps one funder's revocation from stranding another funder's children, and what lets
a branch spend without consulting the other funders.

## Motivation

A spending budget for an AI agent is now a standards-track subject, and every proposal in it assumes
one payer.

[ERC-8427](https://github.com/tankcdr/erc-spend-grants) gives a signed grant from one principal to one
named delegate, with per-call, trailing-window and lifetime caps on each asset. Its `consume` rejects
a redemption by a sub-delegate, and parent-child linkage exists only as an unenforced convention.
ERC-8366 proves one payment against a private policy and records in its rationale that multi-payment
budgets "require monotonic spent-state advanced outside the view-only signature check; they are
deliberately out of scope for this ERC and are expected to build on it". ERC-8312 records an agent's
cumulative spend against a declared bound and states that the interface "does not, on its own, make a
bound impossible for the principal's own key to bypass". ERC-7710 delegation chains narrow authority
per delegation without a shared total.

The theory stops in the same place. Zhu and Wang's *Fault-Tolerant Budget Conservation in Distributed
Multi-Agent Delegation* (arXiv 2610.00349) proves ownership partition, exact conservation and
descendant non-amplification for escrow credits moving through a delegation DAG under crash, retry,
partition and late completion. Its Definition 3 admits a multi-parent join only when every input
carries the same root grant; its Lemma 2 traces every atom to "the unique root credit"; its Theorem 1
is stated per root grant and its Theorem 2 for a single-root sub-DAG. A budget funded by several roots
is outside that model rather than solved by it.

Meanwhile the arrangement is ordinary. A platform and its customer each fund an agent that works for
both. A research grant and a department share a tool budget. Two companies pay into one integration
agent. A marketplace gives every new user a trial allowance that sits beside the user's own balance.
In each case the parties want different rules over their own money, want to withdraw what they have
not spent, and want to know which of them paid for what. Today they either trust one party to hold the
pooled funds and account for it off-chain, or they run separate agents and lose the shared context.

## Specification

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT, RECOMMENDED, MAY and
OPTIONAL in this document are to be interpreted as described in RFC 2119 and RFC 8174.

### Terminology

- **Funder** — an account that supplies capacity. It may be an externally owned account, a contract
  principal validating signatures under [ERC-1271](./eip-1271.md), or a delegated account under
  [EIP-7702](./eip-7702.md).
- **Contribution** — capacity supplied by one funder, in one asset, under one policy, with one
  revocation epoch. A contribution is exclusive: no spend may draw it except one naming it.
- **Pool** — the set of contributions spendable by one spender account.
- **Policy** — the rules a spend must satisfy. This specification does not define a policy language;
  it binds a policy by identifier and requires that it be resolved and enforced before value moves.
- **Spender** — the account authorized to spend a pool. It may itself have delegated narrower
  authority to children; the ancestor rule below governs that case.

### Interface

```solidity
interface IMultiPrincipalBudget {
    struct Contribution {
        address funder;
        address asset;      // ERC-7528 address for native currency
        uint256 remaining;
        bytes32 policy;     // identifier of the policy governing this contribution
        uint64  epoch;      // incremented by revoke; fences in-flight authorizations
        bool    closed;
    }

    event Contributed(
        bytes32 indexed pool,
        bytes32 indexed contribution,
        address indexed funder,
        address asset,
        uint256 amount,
        bytes32 policy
    );

    event Spent(
        bytes32 indexed pool,
        bytes32 indexed contribution,
        address indexed payee,
        address asset,
        uint256 amount,
        bytes32 spendId
    );

    event Revoked(
        bytes32 indexed pool,
        bytes32 indexed contribution,
        uint256 returned,
        uint64 epoch
    );

    function contribute(bytes32 pool, address asset, uint256 amount, bytes32 policy)
        external payable returns (bytes32 contribution);

    function spend(
        bytes32 pool,
        bytes32 contribution,
        address payee,
        uint256 amount,
        uint64 epoch,
        bytes calldata authorization
    ) external returns (bytes32 spendId);

    function revoke(bytes32 contribution) external returns (uint256 returned);

    function contributionOf(bytes32 spendId) external view returns (bytes32);
    function remainingOf(bytes32 contribution) external view returns (uint256);
    function poolOf(bytes32 contribution) external view returns (bytes32);
}
```

A conforming contract MUST implement [ERC-165](./eip-165.md) and return `true` for the interface
identifier `0xb0a522cd`, which is the exclusive-or of the six function selectors above:

```
contribute(bytes32,address,uint256,bytes32)                      0x20e05a57
spend(bytes32,bytes32,address,uint256,uint64,bytes)              0x6032d881
revoke(bytes32)                                                  0xb75c7dc6
contributionOf(bytes32)                                          0x59ad5ac5
remainingOf(bytes32)                                             0xa4cceccc
poolOf(bytes32)                                                  0xba4a6bd4
```

The interface compiles under solc 0.8.30 with no warnings.

### C1. Exclusive capacity

Each contribution's `remaining` MUST be reduced only by a spend naming that contribution. A single
spend MUST NOT draw from more than one contribution. A payment larger than any single contribution's
`remaining` MUST be made as several spends, each naming its own contribution and each separately
attributable.

Implementations MUST NOT provide an operation that spends "from the pool" without naming a
contribution, and MUST NOT select a contribution on the spender's behalf.

### C2. Attribution

Every successful spend MUST emit `Spent` carrying the contribution it drew, and `contributionOf` MUST
return that contribution for the life of the contract. Attribution MUST NOT be derivable only from
event history; it MUST be recoverable from contract state or from a commitment the contract stores.

### C3. Policy binding and ordering

Before any value moves, a spend MUST be checked against:

1. the policy bound to the named contribution, and
2. the policy of every ancestor of the spender in the delegation tree, if the deployment supports
   delegation.

The spend MUST be recorded against the contribution and against every ancestor in the same transaction
as the value movement. A check that passes and a record that is written in a later transaction does not
satisfy this requirement: it is the window in which two concurrent spends both observe the same
remaining.

### C4. Exhaustion isolation

Exhaustion of one contribution MUST NOT change the `remaining` of any other contribution, and MUST NOT
cause a spend naming a different contribution to revert. Revocation of one contribution MUST NOT do
either.

### C5. Revocation and fencing

`revoke` MUST be callable by the contribution's funder. It MUST return the unspent `remaining` to the
funder, set `closed`, and increment `epoch`. A spend whose `epoch` argument does not equal the
contribution's current epoch MUST revert. Revocation MUST NOT reverse, reduce or claw back a spend that
has already settled.

The epoch argument exists so that a spend authorized before a revocation cannot be replayed after it.
A deployment that cannot fence in-flight authorizations MUST NOT claim conformance.

### C6. Assets

A contribution is denominated in exactly one asset. Native currency MUST be identified by the
[ERC-7528](./eip-7528.md) address; every other asset MUST be an [ERC-20](./eip-20.md) contract. A pool
MAY hold contributions in several assets. No implicit conversion between assets is defined; a spend
draws one asset from one contribution.

### C7. Zero

A zero cap means zero. An implementation MUST NOT treat a zero amount, cap or limit as unlimited.

## Rationale

### Attribution rather than conjunction

The obvious reading of "several funders, each with their own rules" is that a spend must satisfy every
contributor's policy at once. That reading is incoherent. Consider a funder that allows payments only
to `arxiv.org` and a funder that allows payments only to an inference provider. Under conjunction the
pool can buy nothing, and the more funders join the less the pool can do. Pools would get worse as they
grew.

Attribution inverts it. A spend draws one funder's capacity and satisfies that funder's policy. The
pool's reach is the union of what its funders permit, not the intersection, and each funder's money is
spent only on what that funder allowed. This is why C1 and C2 are stated before anything else: they are
not bookkeeping, they are what makes a per-funder policy mean something.

### Why capacity is exclusive

Zhu and Wang's Proposition 1 is an impossibility result: in an asynchronous system with a possible
partition, no protocol can guarantee both local availability — either of two isolated branches may
approve a spend of the same currently unallocated unit using only its local state — and global
conservation. A shared unallocated balance is therefore either unavailable during a partition or
unconserved across one.

Exclusive preallocation falsifies the first premise rather than the second: at most one holder has the
right to approve a given unit. That costs utilization, because a funder's idle capacity cannot be
borrowed by a branch that has run out. The alternative is to coordinate on every spend, which on a
single ledger is free — the ledger is the coordinator, and nothing is stranded, because
`remaining` is read and written in the same transaction that moves value.

This specification therefore assumes one ledger and requires coordination per spend (C3), while
structuring capacity exclusively per funder (C1) so that the same object has a meaning when it is later
extended across ledgers. A cross-chain profile would have to preallocate each component's share before
a partition, and is deliberately out of scope here.

### Why this is not an extension of ERC-8427

ERC-8427's grant is a relation between one principal and one named delegate. Its registry records
remaining against that pair, and its `consume` rejects a redemption by a sub-delegate. A pool is a
different object: its identity is the spender, and its capacity has several owners. Bolting several
grants onto one delegate would reproduce the capacity but not C2, C4 or C5, because no participant
could say which grant paid for a given spend, and a revocation would race against spends it has no
relationship to.

The two compose. A contribution MAY be funded by redeeming an ERC-8427 grant, and a deployment that
wants per-call and trailing-window caps over a contribution SHOULD express them in the policy that
contribution binds rather than inventing a second cap vocabulary here.

### Relation to ERC-8366

ERC-8366 proves that one payment satisfies a policy whose parameters stay private, and says that
multi-payment budgets need monotonic spent-state advanced outside its view-only check, deliberately out
of scope and expected to build on it. The `remaining` of a contribution is exactly that monotonic
spent-state. A deployment that wants the per-funder policies to stay private MAY bind `policy` to an
ERC-8366 verifier and keep the parameters off-chain; this specification requires only that the policy be
enforced before value moves, not that it be legible on-chain.

## Backwards Compatibility

No existing interface changes. A pool is a new object. Assets move under [ERC-20](./eip-20.md) and
[ERC-7528](./eip-7528.md) semantics unchanged, and existing allowances are unaffected.

## Reference Implementation

A Python reference and a Solidity port of the single-principal case, with a published specification and
conformance vectors that three independent implementations are tested against, are at
<https://github.com/gazoy/concord>. The multi-principal case specified here is not yet implemented.

## Security Considerations

**Contribution selection is the spender's.** Because the spender names the contribution (C1), a
compromised or faulty spender may drain a permissive funder's capacity first while a restrictive
funder's sits untouched. This specification does not prevent that; it makes it visible, because C2
records who paid for what. Funders who need stronger guarantees should express them in the policy they
bind, not expect the pool to ration between them.

**Revocation races.** A spend authorized before a revocation and submitted after it is the dangerous
case. C5's epoch argument fences it. An implementation that treats a missing or stale epoch as
acceptable reintroduces exactly the race the field has met before, where a timeout or a withdrawal is
mistaken for proof that a payment did not happen.

**Check-then-record windows.** C3 requires the check and the record to share a transaction. The failure
it prevents is live and has been reported in the wild against hand-rolled x402 spend caps, where a
daily cap could be exceeded by concurrent payments because the check and the record straddled an
`await`.

**Policy resolution.** `policy` is an identifier. If the policy it names can change without the
contribution changing, a funder's constraints can be altered after the fact by whoever controls
resolution. An implementation SHOULD bind the policy immutably to the contribution, or resolve it
through a registry whose updates are themselves fenced by the contribution's epoch.

**Re-entrancy.** C3's ordering — check, record, then move value — is also the re-entrancy discipline. A
payee that re-enters observes the already-decremented remaining.

**Partition and multi-chain.** Out of scope, and not safe to assume. See the rationale above and
Zhu and Wang's Proposition 1.

## Copyright

Copyright and related rights waived via [CC0](https://creativecommons.org/publicdomain/zero/1.0/).

Note for this repository: the repository is Apache-2.0, but EIP and ERC text must be CC0. This file
is CC0 and is the only file here under that waiver.
