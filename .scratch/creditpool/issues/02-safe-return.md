## Parent

Part of #1

## What to build

A Team participant can Return unused Credit to the Program Pool. The Operation carries an Idempotency Key and completes via v2 reserve → apply → Reconcile against the Issuer Ledger (tools/ledger enforce money, ADR-0002). Append-only Audit Events are written; the Operations queue shows status. Money tests cover happy-path Return at the Operation-processing seam.

## Acceptance criteria

- [ ] Return creates an Operation and moves Credit from Team to Program Pool on success
- [ ] Available Balance respects active Reservations; apply requires an active Reservation (v2)
- [ ] Audit Events are append-only for the Operation's steps
- [ ] Seam tests pass for happy-path Return without a live LLM

## Blocked by

- Program, Teams, Grants, and read-only wallets (published as blocking issue)
