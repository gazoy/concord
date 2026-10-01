# Review: `docs/try-it.md` and `examples/try_fuji.py`

Read as a stranger: a competent engineer, no prior knowledge of the project, arriving from a link,
deciding in ninety seconds whether to paste three commands into a terminal. Read in the order a
newcomer meets them (`docs/try-it.md`, `examples/try_fuji.py`, `README.md` top), then re-read for
accuracy against `demo/serve_chain.py`, `demo/tap.py`, `foliant/chain.py`,
`docs/spec/spending-policy.md` §§1–7 and `docs/fuji-cost-report.md`. The script was run three times
against a local Anvil chain with the published package, once with `send_raw_transaction`
instrumented.

## Verdict

Yes, I would run it, and that is a genuine compliment — the page is better than almost every crypto
README, because it tells me what I will see before I see it and the thing it demonstrates (a budget
enforced inside the contract, not inside the SDK) is a real claim that most projects cannot make.
Step 6 is the best ninety seconds in the document and it survives scrutiny: I instrumented the
client and confirmed that nothing is broadcast. But I would **not** send it to a colleague today,
for three reasons. First, the page makes two statements that are plainly false against the running
code — "not one of those calls touched the chain" (call 1 broadcasts a `Pools.join` transaction,
finding 1) and "it prints a Snowtrace link for every transaction" (five of the six transactions
print no link at all, finding 2) — and I would be sending a colleague something I had just watched
contradict itself. Second, the headline promise, "settle the whole session in a single
transaction", is the one thing the default run never shows: step 7 prints a promise about a timer
instead of a transaction hash (finding 3). Third, the second run fails (finding 4), which is fatal
for a page whose own troubleshooting section tells you to rerun. None of this is deep: the
protocol appears to do what it says, the gap is between the prose and the script. Fix findings 1–6
and this becomes a page I would send to people, which is not something I say about this genre.

The sentence that would make a sceptical engineer close the tab is not in `try-it.md` at all — it
is in `README.md`, which is where half the traffic will land first:

> This is a **spec plus demo**, not a deployment. It runs in one process with an in-memory ledger
> so the state machines and their invariants can be read, changed and tested quickly.

That tells me it is a toy, in the paragraph immediately before you ask me to run something against
live contracts on a public testnet. It is also, by that point, the fifth paragraph — preceded by a
sixty-word noun pile ("TEE-style attestation, payment channels and streams, Ark-style pooled
channels a whole crew pays through, HTTP 402 metering in the x402 wire format, and the
compute-and-data market objects (whitepaper §6)") and a section about a trade-mark dispute. See
finding 12.

---

## Findings

### 1. High — "not one of those calls touched the chain" is false; the first call broadcasts a transaction

**Location:** `docs/try-it.md:41` (sample output), `docs/try-it.md:58` ("**Step 5 touches no chain
at all.**"), `examples/try_fuji.py:147`.

**Evidence.** `ChainHttpClient` defaults to `prefer_pool=True`
(`foliant/chain.py:650`). On the first 402, `_payment_for` finds no pool claim for the worker and
calls `agent.join_pool(pid, self.default_deposit)` (`foliant/chain.py:680-683`), which goes through
`ChainAgent._send` → `w3.eth.send_raw_transaction` (`foliant/chain.py:271-283`). Instrumenting
`send_raw_transaction` shows the broadcast landing inside step 5, immediately before `call 1`
prints:

```
5. Make three paid API calls
   [INSTRUMENT] send_raw_transaction #5 -> 6781e9b9a7c963ad2c
   call 1    paid 3 units, answered 'call 1'
   ...
   3 signed receipts, and not one of those calls touched the chain
```

The script then prints its own refutation nine lines later: `worker committed 100.00 of its 300
this hour`. That 100 is the pool deposit committed during step 5 — delegation funding is not a
spend (`spending-policy.md` §4.3), so there is nowhere else it can have come from. The spec agrees:
"Opening a channel or joining a pool is a spend of the deposit" (§7.1).

This is the single most damaging line in the document, because it is the claim a sceptic is
specifically looking for and it is falsified by the script's own output two inches below it.

**Fix.** Say what is true, which is more interesting anyway: the first call opens the funding
position on chain, and every call after it is free. Change line 41 to
`3 signed receipts — one on-chain transaction for the first call, none for the rest`, print the
join transaction with the existing `tx()` helper when it happens, and rewrite line 58 as
"**Step 5 puts one transaction on chain, for the first call only.** The worker joins the provider's
pool once; the three payments are HTTP 402 exchanges in the x402 wire format…". Raising the call
count (finding 7) makes this reframing land harder, not softer.

### 2. High — "prints a Snowtrace link for every transaction" is false; five of six print nothing

**Location:** `docs/try-it.md:18-19`, `examples/try_fuji.py:10-11` ("Every step prints the
transaction so you can check it on Snowtrace yourself").

**Evidence.** The instrumented run broadcasts six transactions: `register`, `approve`, `deposit`
(step 3), `delegate` (step 4), `join` (step 5), plus the tap's two. Only the tap's two reach
`tx()` (`examples/try_fuji.py:100-101`). Steps 3, 4 and 5 print a policy summary and an account id
and no hash whatsoever — as the document's own "What you should see" block shows (lines 31-41),
contradicting its prose on line 18. The claim is attached to the sentence "so nothing here has to
be taken on trust", which makes the miss pointed rather than cosmetic.

**Fix.** Have `ChainAgent._send` return the receipt (it already does) and print `tx("registered",
r["transactionHash"])`, `tx("deposited", …)`, `tx("delegated", …)`, `tx("joined the pool", …)`.
This is roughly four lines in the script and it converts the page's central promise from a claim
into a fact. It is also the cheapest fix in this review for the most credibility gained.

### 3. High — the headline promise, "settle … in a single transaction", is never demonstrated

**Location:** `docs/try-it.md:3-5` (intro), `docs/try-it.md:46` and `examples/try_fuji.py:159-172`
(step 7).

**Evidence.** The intro promises you will "settle the whole session in a single transaction". Step
7 only settles when `FOLIANT_SETTLE_KEY` is set (`try_fuji.py:163`), and `/settle` requires that key
(`demo/serve_chain.py:155-157`). No visitor has it. So every reader gets:

```
7. Settle the session — one transaction for all of it
   the public server settles on a timer, so this run's payments go out within ten minutes.
```

A heading asserting a transaction, followed by no transaction. The timer itself is real for the
public deployment (`deploy/foliant-settle.timer`, `deploy/README.md:4`), so this is not a lie — but
it is the one claim in the intro that the run does not back, it is unverifiable from the terminal,
and it asks for exactly the trust the page says it does not need.

Worse, the message is hardcoded regardless of `SERVER`. Running against your own server as the page
instructs (`FOLIANT_DEMO=http://localhost:8402`, line 86), you are told "the public server settles
on a timer" — about a local server that has no timer. I saw this printed verbatim in my local run.

**Fix.** Two options, both small. Either drop "and settle the whole session in a single
transaction" from the intro and retitle step 7 "Where the settlement happens", or — better — have
the script poll the pool's on-chain state for up to ~60s and print the settlement transaction when
the timer fires, so the promise is actually paid. At minimum, make the message conditional: say
"the server settles on a timer" only when `SERVER` is the default, and for a self-hosted server
print `POST /settle` with the key instead.

### 4. High — the second run fails at step 5 with an unreadable error dump

**Location:** `examples/try_fuji.py:142-146`; `docs/try-it.md:94-96` actively invites a rerun
("wait, or fund an address yourself … and rerun").

**Evidence.** Three consecutive runs against the same server and the same `~/.foliant-try.json`:
run 1 succeeds; runs 2 and 3 die at step 5:

```
5. Make three paid API calls

call 1 was not served: 402 {"x402Version":1,"accepts":[{"scheme":"foliant-channel","network":"anvil",
"payTo":"0x70997970C51812dc3A010C7d01b50e0d17dc79C8","asset":"0xDc64a140Aa3E98110…
```

Cause: a fresh `ChainHttpClient` has no `latest` for the pool, so `pay_pool` restarts the sequence
from the on-chain `claim["seq"]` (`foliant/chain.py:385-388`), which the provider has not settled
yet, so the gate rejects every attempt as `stale update`. The retry path burns its two allowed
incidents (`foliant/chain.py:706-712`) and gives up. The server log confirms three straight 402s
where run 1 had 402/200 pairs. The window closes only once the settle timer has run.

Two compounding problems on the same path:

- **Step 3 re-deposits 500 tokens on every run.** `if held < 400 * UNIT` (`try_fuji.py:119`) reads
  only the root's own balance and ignores the 300 already delegated to the worker. Run 1 leaves the
  root at 200, so run 2 deposits another 500, and run 3 another. By run 3 the wallet reads `balance
  … 0.00 tokens`; a fourth run reverts.
- **There is no way back.** The tap funds an address once, ever (`demo/tap.py:206`), so the
  exhausted key cannot be refilled. Deleting `~/.foliant-try.json` gets a new key but the
  per-client cap is three a day (`per_client_day=3`, `demo/tap.py:497`), as the page itself says —
  so a stranger who reruns a few times is locked out until tomorrow, having seen the demo work
  once and fail three times.

**Fix.** Make the rerun path work, which is mostly one change plus one guard: (a) in step 3,
compute `held` as the root's balance *plus* the worker's, or simply skip the deposit when the
worker already holds ≥ 300; (b) on a `stale update` 402 at step 5, catch it and print a readable
message — "this session's earlier payments have not settled yet; wait for the settle timer (about
ten minutes) and rerun, or delete `~/.foliant-try.json` for a clean start" — rather than dumping a
truncated JSON body; (c) add that message to the troubleshooting list. Also truncate or
pretty-print the 402 body in `fail()` either way.

### 5. High — `pip install` as written fails on stock Ubuntu/Debian, and no Python version is stated

**Location:** `docs/try-it.md:12`, `README.md:31`.

**Evidence.** `pip install "foliant-protocol[chain]" httpx` on Ubuntu 23.04+ / Debian 12+ with the
system Python returns `error: externally-managed-environment` (PEP 668) and installs nothing. This
is the default state of the most common Linux desktop and the most common cloud image; a large
fraction of the target audience cannot get past line 1. Separately, `pyproject.toml:11` sets
`requires-python = ">=3.11"`, which appears nowhere in the reader's path — on Ubuntu 22.04
(Python 3.10, still widely deployed) pip fails with "Could not find a version that satisfies the
requirement", which reads like a broken package rather than a version floor.

**Fix.** Four lines instead of three, and state the floor:

```bash
python3 -m venv foliant-try && . foliant-try/bin/activate   # Python 3.11+
pip install "foliant-protocol[chain]" httpx
curl -fsSLO https://raw.githubusercontent.com/gazoy/concord/main/examples/try_fuji.py
python try_fuji.py
```

Note `-fsSLO` rather than `-O`: with a bare `-O`, a 404 or a proxy error page is written to
`try_fuji.py` and curl exits 0, so the next line fails with a syntax error in a file the reader
believes is yours. `-L` also matters if the repository is ever renamed (it is `gazoy/concord` while
the project is called Foliant — that redirect is one rename away from being load-bearing).

### 6. Medium — "nothing here can cost you anything" is unguarded, and the script knows about mainnet

**Location:** `docs/try-it.md:7`; `examples/try_fuji.py:24` (`EXPLORERS = {43113: …, 43114:
"https://snowtrace.io"}`).

**Evidence.** The script takes its RPC endpoint, contract addresses and token from whatever server
`FOLIANT_DEMO` names (`try_fuji.py:91-93`), never checks the chain id, and carries an explorer entry
for **Avalanche mainnet (43114)**. The page then explicitly invites you to point it elsewhere
("Running it against your own server", line 80). So the blanket statement on line 7 is a property
of the default URL, not of the script, and the script is visibly prepared for the case where it is
false. A hostile reader will find `43114` in thirty seconds and call the opening claim marketing.

**Fix.** Make the claim true by enforcing it: after step 1, `if chain["chainId"] not in (43113,
31337) and not os.environ.get("FOLIANT_ALLOW_MAINNET"): return fail(...)`. Then line 7 is a
guarantee rather than an assurance, and the mainnet explorer entry stops being evidence against
you.

### 7. Medium — the demo runs below its own break-even, and the economics claim rests on an unmeasured estimate

**Location:** `docs/try-it.md:66-68`; `docs/fuji-cost-report.md`.

**Evidence.** The two figures quoted are accurate against the report: break-even "about 5 calls on
the pool route and 7 on the channel route", and the 100-call row reads "93–96 %". But:

- **The session the reader runs is three calls.** The script uses the pool route, and the report
  puts the pool route at ≈335,000 gas per session against ≈75,000 per call for x402 `exact`. Three
  calls is 335k versus 225k — by the project's own numbers, the demonstration session costs about
  1.5× the thing it is beating. A reader who follows the cost-report link and does the arithmetic
  finds the demo sitting below its own break-even. That is an unforced error.
- **"The measured cost report"** overstates the baseline. The Foliant side is measured from
  receipts; the comparison side is not — the report says so plainly: "The `exact` figure is an
  estimate for an EIP-3009 `transferWithAuthorization` on Avalanche … it is not measured here".
  The saving is therefore measured-over-estimated, and calling it "measured" without qualification
  is the second thing a hostile reader will reach for after finding 1.
- The report's scenario is the **channel** route (worker opens a 100 channel; orchestrator joins the
  pool); the script is the **pool** route with the worker joining. A reader cross-referencing the
  two finds different actors doing different things under the same name.

**Fix.** Raise the call count to something above break-even — 25 calls costs the same on chain and
nothing in wall-clock, and makes "thousands of calls cost the same on-chain as three" demonstrated
rather than asserted. Reword line 67-68 to "puts the break-even at five to seven calls and the
saving above 90 % at a hundred, against an estimated per-call `exact` baseline". The honesty costs
you nothing and buys the rest of the page credibility.

### 8. Medium — step 4's strongest sentence describes something the run does not do

**Location:** `docs/try-it.md:51-56`.

**Evidence.** "A worker cannot exceed its budget, and a crew of workers cannot exceed the
orchestrator's, even if the orchestrator tightens its policy after the workers were created."

The property is **true and properly specified** — `spending-policy.md` §4.2 states it explicitly
("it holds even if a child holds a policy wider than its parent's (for example after the parent was
tightened)") and the README lists it among the tested tree invariants. But the paragraph sits under
the heading "What each step is actually showing", and the run shows neither half: there is one
worker, not a crew, and nothing is ever tightened. A reader who takes the heading literally — which
is the reader this page is written for — looks for it in the output and does not find it.

**Fix.** Either demonstrate it (delegate a second worker with the same salt+1, then `setPolicy` the
root down to 50 and watch the already-funded worker's next commit refuse — perhaps 15 lines and one
more transaction, and it would be the best thing on the page), or move the sentence out from under
"what each step is showing" and mark it as a specified and tested property with the §4.2 link, so
the reader knows where to check it instead of looking for it in their terminal.

### 9. Medium — "units" means two different magnitudes in the same output

**Location:** `examples/try_fuji.py:73, 117, 134, 146`; sample output `docs/try-it.md:26-45`.

**Evidence.** Step 1 prints `price 3 units per call` — raw smallest units. Steps 3 and 4 print
`policy 500 per payment, 2000 per hour` and `100 per payment, 300 per hour` — whole tokens, i.e.
`500 * UNIT` = 500,000,000 of the units in step 1. A reader doing the obvious arithmetic concludes
the worker can afford 33 calls before hitting its per-payment cap, when the true figure is about 33
million. The closing summary then compounds it: `worker committed 100.00 of its 300 this hour`, in
tokens, against three calls priced in units.

This matters more than it looks: the units confusion is exactly what makes finding 7's break-even
question hard to check, and it is the sort of thing that makes an engineer wonder what else is
being glossed.

**Fix.** Pick one unit for display. Print `price 0.000003 tokens per call (3 units)` at step 1, or
label the policy lines `500 tokens per payment`. The `tokens()` helper already exists; use it
consistently and say "tokens" wherever it is used.

### 10. Medium — failures outside the two handled calls produce raw tracebacks

**Location:** `examples/try_fuji.py:92` and throughout.

**Evidence.** Only `GET /chain` and `POST /tap` are wrapped. Everything else fails with an
unhandled stack trace:

- `ChainLedger(chain["rpc"], …)` (line 92) calls `self.w3.eth.chain_id` in its constructor
  (`foliant/chain.py:116`). The RPC host is a **different host from the demo server** — the public
  demo will name an Avalanche RPC endpoint — so a corporate proxy or firewall that permits
  `fuji.foliant.network` can still block it. The reader gets a `requests` traceback and the
  troubleshooting section's "cannot reach https://fuji.foliant.network" entry does not apply,
  because they *could* reach it. The page never says you need outbound access to a second host.
- A corrupt or truncated `~/.foliant-try.json` → `JSONDecodeError` at line 77.
- Step 5 raises `PolicyViolation` out of `client.request` if a budget is hit (it catches only
  non-200 status codes), and step 6 catches `PolicyViolation` only — an `InsufficientFunds` or a
  node that does not return revert data for `eth_estimateGas` escapes as a traceback.

**Fix.** Wrap `main()`'s body in `try/except FoliantError as e: return fail(str(e), …)` and wrap the
`ChainLedger` construction with its own message naming the RPC host: "cannot reach the chain node
the server named ({rpc}) — your network may allow the demo server but not the RPC endpoint". Add
that host to the "If something goes wrong" list.

### 11. Medium — the troubleshooting list misses the two messages a stranger is most likely to see

**Location:** `docs/try-it.md:92-102`.

**Evidence.** Two people running at once hit `TapError(503, "the tap is serving someone else; retry
in a few seconds")` (`demo/tap.py:186`), which the script treats as fatal and whose hint text talks
about the tap being "dry or busy, try again later" — close but not matching. Equally likely are
`"the tap is busy; try again in an hour"` (hourly cap, `demo/tap.py:215`) and
`"this address has already been funded (or an attempt was made)"` (`demo/tap.py:205`), which is what
a reader gets if they delete the state file and restore it, or run from two machines. None of the
three is in the list. Add the stale-update failure from finding 4 as well.

Separately: the script's own fallback hint says "see `demo/serve_chain.py` in the repository"
(`try_fuji.py:68`) — but the reader was told to `curl` a single file and has no repository, and the
page itself points at `deploy/README.md` instead (line 89). Make the two agree and give a URL.

### 12. Medium — `README.md` buries the only actionable thing and undercuts it first

**Location:** `README.md:1-35`.

**Evidence.** A stranger landing on the README meets, in order: a 60-word noun pile ("TEE-style
attestation, payment channels and streams, Ark-style pooled channels a whole crew pays through …
and the compute-and-data market objects (whitepaper §6)"); a paragraph about what Foliant is *not*;
"This is a **spec plus demo**, not a deployment. It runs in one process with an in-memory ledger";
and then a section explaining that the project used to be called Concord after a trade-mark search.
"## Try it" is fifth.

The in-memory-ledger sentence is accurate about `foliant/ledger.py` and badly wrong as positioning:
it is immediately followed by instructions to run against deployed contracts on Fuji, and it is the
sentence a sceptic quotes when they close the tab. The trade-mark section is of interest to exactly
zero first-time readers and costs you the fold.

**Fix.** Lead with one concrete sentence (something close to `try-it.md`'s intro — "give a worker
agent an on-chain spending budget, watch it pay for API calls off chain, watch the chain refuse the
one commitment the budget does not allow"), then the Try-it block, then the architecture prose.
Move "## Name" below "## Documents". Reword the in-memory sentence to scope it: "The Python
reference runs in one process with an in-memory ledger, so the state machines can be read and
tested quickly; the Solidity port implements the same state machines and is deployed on Fuji."

### 13. Medium — README claims 115 vectors "run against both references"; about 66 run against the EVM one

**Location:** `README.md:43` ("115 conformance vectors run against both references").

**Evidence.** `vectors.json` holds exactly 115 vectors (`check` 31, `x402-exact` 35, `within` 22,
`policy` 9, `tree` 8, `window` 6, `id` 4) — so `try-it.md`'s plainer "115 conformance vectors" is
**correct** and needs no change. But the spec's own conformance section says the EVM reference
"runs `check`, `within`, `window`, `tree` and `policy` … and skips `id` (it hashes a struct) and
`x402-exact` (no on-chain counterpart)", plus three zero-amount `check` vectors, `check-030`,
`policy-003` and `policy-009`. That is 39 vectors skipped by design, out of 115. The spec is
scrupulous about this; the README sentence erases it.

**Fix.** "115 conformance vectors, all of which the Python reference runs and 76 of which apply to
the EVM reference (the rest are skipped for stated reasons)". The spec already has the honest
wording — reuse it.

### 14. Low — step 6's "no gas is spent" is a property of the SDK, not of the chain

**Location:** `docs/try-it.md:62-64`; `examples/try_fuji.py:156`.

**Evidence.** The substantive claim **holds**, and I verified it two ways. `ChainAgent._send` calls
`fn.build_transaction(...)` (`foliant/chain.py:275`), which estimates gas; the revert is decoded to
`PolicyViolation` by `_decode_revert` before `send_raw_transaction` is reached
(`foliant/chain.py:276-279`), and the comment on line 277 says exactly that. The instrumented run
shows **no** `send_raw_transaction` during step 6. And the check really is a contract precondition,
not a wrapper: `PaymentChannels.open` calls `accounts.commit`
(`contracts/src/PaymentChannels.sol:93`), which reverts with
`PolicyViolation("amount exceeds per_tx_max")` at `contracts/src/AgentAccounts.sol:488` — the exact
string the page prints. So step 6 is sound and the strong sentence ("not a wrapper the agent's code
can route around, it is the contract's precondition") is earned.

The nuance: "no gas is spent" is true because *this client estimates first*. An agent that set an
explicit gas limit and broadcast anyway would have the transaction revert on chain and pay for it.
The safety property is unaffected — the spend never happens — but the gas sentence describes the
SDK.

**Fix.** One clause: "The attempt fails at estimate time, so this client sends nothing and spends no
gas; an agent that forced the transaction through would simply have it reverted by the contract and
pay for the attempt." This makes the claim stronger, not weaker — it says the enforcement does not
depend on the client behaving.

### 15. Low — script readability: dead paths, drifting literals, and ANSI codes

**Location:** `examples/try_fuji.py`.

People will read this instead of the docs, so it is documentation. It mostly reads well — the
section rules, the comment at line 126-127 explaining why one key operates both accounts, and the
comment at 160-161 on who may settle are all exactly right. Specifics:

- **Step 7's `FOLIANT_SETTLE_KEY` branch (163-169) is dead for every reader** and the variable is
  undocumented in `try-it.md`. It is 7 lines of the most important step that nobody will execute.
  Either document the variable in the "your own server" section or move the branch behind a
  one-line helper so the main flow reads cleanly.
- **Hardcoded numbers duplicated in prose.** Line 117 prints `500 per payment, 2000 per hour` as an
  f-string with no placeholders (so a linter flags it), restating the literals from line 116; same
  at 134 versus 132-133. These will drift. Interpolate from the `ChainPolicy` object.
- **Magic salts.** `salt=1` (line 133) and `salt=7` (line 152) are unexplained; `salt=7` in
  particular looks meaningful and is not.
- **Inconsistent account-id handling.** `boss.account_id = root` is assigned by reaching into the
  object (line 112) while `worker` gets `account_id=` as a constructor argument (line 135), eleven
  lines apart. Use the constructor both times.
- **`fail()` is defined after `main()`** but called six times inside it, the first 120 lines before
  the definition. Move it above `main`.
- **Bare ANSI escapes** (`\033[1m`, `\033[31m`) with no `isatty()` guard: piping the output to a
  file or running in a terminal without ANSI support (Windows `cmd.exe`) litters it with `[1m`. The
  page offers only a bash block, so Windows readers are already second-class — a two-line guard
  fixes both.
- `acct.key.hex() if acct.key.hex().startswith("0x") else "0x" + acct.key.hex()` (line 83) is
  defensive against eth-account versions; worth a five-word comment saying so, otherwise it reads
  as confusion.
- **Output string that will puzzle:** `price 3 units per call, paid in 0xDc64a140…` — a truncated
  token address with no label. Say `paid in token 0xDc64a140…`.

### 16. Low — the key file: a private key in `$HOME`, mentioned only in troubleshooting

**Location:** `examples/try_fuji.py:77-86`; `docs/try-it.md:95`.

`~/.foliant-try.json` holds a private key. The script's docstring says it "is worth nothing" (true
on testnet); `try-it.md` mentions the file only inside a troubleshooting entry, so a reader who has
no trouble never learns it exists. Say so in the "Run it" section — one clause, "it writes a
throwaway key to `~/.foliant-try.json`; delete it when you are done" — because an engineer who
discovers an unannounced key file in their home directory later will think worse of the project
than one who was told.

Minor correctness nit on the same lines: `STATE.write_text(...)` then `STATE.chmod(0o600)` leaves a
window where the key sits at the umask default. Create with `os.open(..., 0o600)` instead.

### 17. Low — the sample output does not match the real output

**Location:** `docs/try-it.md:23-47`.

Step 1 omits the `accounts  0x…` line and the `, paid in 0x…` suffix that the script actually
prints; step 2 shows one `funded:` line where the tap sends two transactions (mint and AVAX) and
the script prints both; the `balance` line is absent entirely. For a page whose pitch is "check it
yourself", the block headed "What you should see" should be a transcript, not a paraphrase.
Regenerate it from a real run (and, after findings 1-3, it will contain the transaction links that
make the point).

---

## What is missing in the first ninety seconds

For *this* audience — an engineer who has never heard of the project — in rough order of how much
each would change the run/don't-run decision:

1. **One sentence saying what Foliant is**, before "Try Foliant on Avalanche Fuji". The page opens
   assuming the name means something. It does not. Something like: "Foliant gives an AI agent an
   on-chain spending budget its own code cannot exceed, and lets it pay for API calls without a
   transaction per call."
2. **What this does to my machine and my network.** It installs web3 and eth-account (large), writes
   a key to `$HOME`, and needs outbound HTTPS to *two* hosts — the demo server and whatever RPC
   endpoint it names. The second one is invisible until it fails (finding 10).
3. **Python version and a venv line** (finding 5).
4. **How long it actually takes.** "Ten minutes" is the headline; the run is about 60-90 seconds
   plus install. Say so — it is better than advertised and it tells me the ten minutes includes
   reading.
5. **A transcript or asciinema for the 80 % who will not run it.** The README has a demo GIF;
   `try-it.md` does not link it. Most readers decide from the page alone, and a real transcript
   with live Snowtrace links is the cheapest trust you can buy.
6. **One sentence on why not plain x402 `exact`.** The whole value proposition is a comparison, and
   it is only reachable via a link to the cost report. "Per-call x402 puts a transaction in every
   call's latency path; Foliant puts one at the start of the session" belongs on this page.
7. **What it costs to be wrong.** The contracts have three audits (`contracts/audits/`) and the spec
   has an independent review — both are mentioned under "Then what", at the bottom. For a sceptic,
   "audited, and here is the review with every finding recorded" is a ninety-second fact, not a
   footnote.

---

## What works well — do not change these

- **Step 6 is the best thing here, and it survives scrutiny.** I went looking for a reason to call
  it theatre and did not find one: the policy check is in `AgentAccounts.commit`, the failure is at
  estimate time, nothing is broadcast, and the printed string is the contract's own revert reason.
  Building the whole walkthrough around it is the right call.
- **"What you should see" before "Run it".** Showing the output before asking for the command is
  the single most reader-respecting decision on the page. Most projects make you run it to find
  out. Keep the structure even when you regenerate the content (finding 17).
- **"What each step is actually showing".** Separating *what happens* from *why it matters* is
  exactly right, and the step-by-step bolded leads are easy to skim in ninety seconds.
- **The honesty that is already there.** "only the provider can settle on demand; a visitor waits
  for the timer, which is the honest picture anyway" (`try_fuji.py:160-161`); the cost report
  stating that the `exact` baseline is an estimate and listing "What this run did not measure"; the
  spec stating exactly which vectors each reference skips and why. This instinct is the project's
  best asset — findings 1, 2, 3 and 7 are all cases where the prose fell below the standard the
  rest of the repo sets, not cases where the standard is absent.
- **The closing invitation**: "Anything else, including anything that worked but read badly: please
  open an issue. This page is meant to work first time for someone who has never seen the project."
  That paragraph is why this review exists and it is worth keeping verbatim.
- **The server naming its own contracts, RPC and token**, so the same script works against any
  Foliant server. That is a genuinely good design decision and the `FOLIANT_DEMO` escape hatch is
  the right shape — it just needs the chain-id guard from finding 6.
- **The tap.** `demo/tap.py` is unusually careful — per-address, per-client, per-hour and per-day
  limits all charged before anything is broadcast, the precompile and contract-recipient probes,
  the stuck-transaction record. It is not reader-facing, but it is why "no faucet" is true at all.
