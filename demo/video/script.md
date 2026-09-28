# Foliant demo — voiceover script
Read each line when its timestamp appears; the caption on screen matches.

**0:00** — This is the Foliant reference implementation. One command runs four scenarios in about a second. I'll talk through what each one shows.

**0:11** — Scenario A: one agent, one payment channel, twenty API calls. Look at the third line: two chain transfers in total, the channel open and, later, the settlement. Twenty calls, zero per-call transactions.

**0:28** — The provider settles once for sixty units, and the agent is holding twenty signed receipts, one per call, that prove what it bought.

**0:39** — Scenario B: three agents share the provider's pool, ten calls each. Thirty calls settle in one transaction, and the pool tracks what each member owes. This is a crew of agents paying one provider.

**0:53** — Scenario C is the one that matters. The operator set a cap of two hundred units per hour. Sixty-six calls in, the agent's signer refuses to commit any more, and the ledger's view agrees to the unit. A crew gets a budget, not a hot wallet.

**1:09** — Scenario D: what if the provider vanishes? Bob exits the pool on his own and gets his unspent seventy back after the timeout. Nobody's funds depend on the coordinator being honest or online.

**1:22** — And the supply check: every unit is accounted for.

**1:27** — Twenty-two tests back this, including property-based checks over hundreds of random sessions: a payee never gets more than the payer signed, a payer always gets the rest back, and the policy window is never exceeded. The code is public at github.com/gazoy/concord.

Total length: 1:52. Record in one take with Voice Memos or QuickTime while the video plays; send me the audio file and I'll merge it.
