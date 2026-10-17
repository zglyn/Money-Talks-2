## Parent

Part of #1

## What to build

Agent Version v1 can skip/weakly check reservation and Program Rules so concurrency and over-cap failures reproduce. PRISM Alert fires on failed reconcile or apply_reallocation; Root Cause clusters the v1 failure; the same inputs pass on v2 with Flagged risky moves instead of ledger mismatch.

## Acceptance criteria

- [ ] v1 batch produces visible money failures (e.g. double-spend / reconcile mismatch)
- [ ] Alert configured and shown firing on a real failed tool call
- [ ] Root Cause cluster supports the cause sentence about applying without reservation
- [ ] Same seed inputs on v2 show no ledger mismatches; risky moves Flagged

## Blocked by

- Agent worker v2 + Nicrron API + PRISM Sessions
