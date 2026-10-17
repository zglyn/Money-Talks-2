## Parent

Part of #1

## What to build

Demo control can seed sample data, snapshot/reset the Issuer Ledger, toggle v1/v2, and run the seeded Operation batch. Reconciliation page highlights per-Team mismatches; Audit log is filterable by Operation. End-to-end path supports the 3-minute demo script.

## Acceptance criteria

- [ ] Seed creates Program(s), Teams, Grants, and the agreed scenario Operations
- [ ] Snapshot/reset restores ledger state for v1 then v2 comparison
- [ ] Reconciliation UI shows red mismatches when present; clean after v2
- [ ] Audit log filterable by Operation; demo path runnable without manual SQL

## Blocked by

- Request from Pool and Admin Team→Team Reallocate
- Concurrency, idempotent retry, and stale Reservation
- Agent Version v1 failure + PRISM Alert / Root Cause
