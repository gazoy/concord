# Foliant contracts

Solidity port of the reference implementation in `../foliant/`. Foundry project.

| Contract | Reference | Status |
| --- | --- | --- |
| `AgentAccounts` | `accounts.py`, account half of `ledger.py` | 55 tests (unit, fuzz, invariant, window-vs-reference), Slither clean, [AUDIT-1](audits/AUDIT-1.md): 12 findings, all closed over three rounds |

```
forge build && forge test
```

Requires a native `solc` 0.8.30 at the path named in `foundry.toml` (the session cannot reach the solc download host; a GitHub release binary is used). Dependencies: forge-std, OpenZeppelin 5.4 (`forge install`, see `remappings.txt`).
