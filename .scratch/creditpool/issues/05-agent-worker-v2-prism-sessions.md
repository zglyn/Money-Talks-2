## Parent

Part of #1

## What to build

A Python worker claims requested Operations, runs the v2 agent tool order via the Nicrron API, traces the run in PRISM (6+ steps), and stores the agent run plus PRISM trace link on the Operation. Tools own arithmetic; the model only sequences and explains.

## Acceptance criteria

- [ ] Worker claims Operations with no double-claim under concurrency
- [ ] A clean v2 run shows get_operation → rules → balance → eligibility → reserve → apply → Reconcile → audit (6+ steps) in PRISM
- [ ] Operation links to agent_runs / PRISM trace URL
- [ ] All model calls use the Nicrron API (not treated as Issuer Ledger)

## Blocked by

- Safe Return to Program Pool (v2 money path)
