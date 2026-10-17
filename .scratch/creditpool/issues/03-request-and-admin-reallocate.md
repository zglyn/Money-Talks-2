## Parent

Part of #1

## What to build

A Team can Request Credit from the Program Pool, and an Admin can Reallocate Team → Team in the same Program. Program Rules refusals (per-Team cap, eligibility, expiry, cross-Program) Flag the Operation with no partial Credit create. Seam tests cover these refusal paths.

## Acceptance criteria

- [ ] Request is a Reallocate from Program Pool to the requesting Team
- [ ] Admin Team → Team Reallocate works inside one Program
- [ ] Cap, eligibility, expiry, and cross-Program violations Flag (or fail) with unchanged illegal ledger apply
- [ ] Seam tests cover Request happy path and the refusal cases above

## Blocked by

- Safe Return to Program Pool (v2 money path)
