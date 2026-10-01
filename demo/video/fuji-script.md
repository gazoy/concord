# Narration for the Fuji walkthrough video

Recorded 1 October 2026, 85 seconds. `demo/make_fuji_video.py` times the terminal reveal to these
paragraphs; its `MARKS` table holds the boundaries measured from the recording.

**Title card**

> An AI agent that buys data or compute pays per call. Today that means a hot wallet, and a spending
> limit written in the agent's own code — which the agent can route around.
>
> Foliant puts the budget where the money is.

**The run**

> Here I'm a stranger with nothing. No wallet, no test funds. The demo server funds me.
>
> The orchestrator registers an account. Five hundred a payment, two thousand an hour.
>
> It delegates a worker — twenty a payment, sixty an hour — inside its own budget. One transaction,
> and no human in the loop.
>
> The worker commits twenty to the provider's pool. That single transaction is the payment the
> policy checks.
>
> Then twenty-five paid API calls. Watch the transaction count. Zero. These are signed off chain, in
> the x402 format, and bounded by the deposit.
>
> Now the worker tries to commit thirty — half as much again as its cap.
>
> The contract refuses it. Not a wrapper, not a library check: a precondition of the call that holds
> the funds. Nothing was sent, and no gas was spent.
>
> The provider settles the whole session in one transaction.

**Closing card**

> Twenty-five calls. Two transactions. A budget the agent cannot exceed.
>
> It's live on Avalanche Fuji, it's audited, and you can run exactly this yourself in ten minutes.
