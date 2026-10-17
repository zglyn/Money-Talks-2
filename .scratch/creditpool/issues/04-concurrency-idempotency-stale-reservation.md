## Parent

Part of #1

## What to build

Under concurrency and failure, v2 cannot create extra purchasing power: concurrent drains on one Team, duplicate Idempotency Keys, and stale Reservations after crash/timeout all leave Available Balance ≥ 0 and at most one apply per logical move.

## Acceptance criteria

- [ ] Two concurrent Operations draining the same Team: one applies, the other Flags/fails at reserve; Available Balance never below zero
- [ ] Duplicate Idempotency Key returns the original Operation with no second ledger move
- [ ] After reserve without apply (crash/timeout), Reservation expires/releases and no Credit is applied until a new successful path

## Blocked by

- Safe Return to Program Pool (v2 money path)
