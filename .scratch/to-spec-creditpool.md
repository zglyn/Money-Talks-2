## Problem Statement

Hackathon Issuers (e.g. Nicrron at MONEY TALKS 2) grant each Team Credit that expires on a hard deadline. Much of that Credit sits unused on some Teams while others run dry. Reselling or pooling API keys breaks upstream terms. The Issuer already owns the ledger and the rules, but has no safe workflow to Return unused Credit into a Program Pool and Reallocate it to Teams that need more — with Reservations, Reconcile, and an audit trail an agent can run without creating purchasing power by mistake.

## Solution

Build **CreditPool**: an Issuer-controlled tool for one Program that lets Teams Return Credit to a Program Pool and Request Credit from it, lets an Admin Reallocate Team → Team, and runs each Operation through an agent that only sequences steps while tools and the simulated Issuer Ledger enforce Program Rules. A naive Agent Version (v1) demonstrates double-spend failures; v2 proves the fix for PRISM. The demo does not move real Nicrron balances (ADR-0001).

## User Stories

1. As an Admin, I want to configure one Program with total budget, per-Team cap, expiry, and whether Returns are allowed, so that Program Rules are explicit before any Credit moves.
2. As an Admin, I want to register eligible Teams for a Program, so that only valid recipients can receive Credit.
3. As an Admin, I want a Program Pool Team created with the Program, so that Returned Credit has a single holder inside the same balance model.
4. As an Admin, I want to Grant initial Credit to each Team on the Issuer Ledger, so that the Program starts with known balances.
5. As an Admin, I want to see budget used vs remaining on a Program console, so that I know how much Credit is still allocatable.
6. As a Team participant, I want to see my Team's ledger balance and Available Balance, so that I know what I can Return or spend against Reservations.
7. As a Team participant, I want to Return unused Credit to the Program Pool, so that surplus is available to others before expiry.
8. As a Team participant, I want to Request Credit from the Program Pool, so that my Team can receive more without an Admin picking a source every time.
9. As an Admin, I want to Reallocate Credit directly from one Team to another in the same Program, so that I can rescue a Team without waiting for Returns.
10. As any actor creating an Operation, I want each mutation to carry an Idempotency Key, so that retries do not create a second ledger move.
11. As a Team participant, I want a duplicate Return or Request with the same Idempotency Key to return the original Operation, so that double-clicks are safe.
12. As the system, I want Available Balance to equal Issuer Ledger balance minus active Reservations, so that concurrent Operations cannot double-spend.
13. As the system, I want reserve to fail when Available Balance is insufficient, so that overdraw never reaches apply.
14. As the system, I want apply to refuse without an active Reservation for that Operation (v2), so that apply cannot race ahead of a hold.
15. As the system, I want apply to refuse when the recipient would exceed the per-Team cap, so that Program Rules hold.
16. As the system, I want apply to refuse when the recipient Team is ineligible, so that Credit cannot leave the eligibility list.
17. As the system, I want apply to refuse after Program expiry, so that late moves cannot create Credit past the deadline.
18. As the system, I want apply to refuse cross-Program Team references, so that Programs stay separate namespaces.
19. As the system, I want the Program total across the Issuer Ledger never to exceed total budget, so that Grants and moves cannot inflate supply.
20. As an Admin, I want concurrent Operations draining the same Team to result in at most one apply and the other Flagged or failed at reserve, so that Available Balance never goes below zero.
21. As an Admin, I want a Flagged Operation when a rule check fails, so that I can review it without a half-applied Credit create.
22. As an Admin, I want Reconcile after apply to verify Issuer Ledger, Operations, and Reservations agree, so that mismatches surface as money failures.
23. As an Admin, I want a Reconcile mismatch to mark the run failed and be alert-worthy, so that ledger drift is never a soft warning.
24. As an Admin, I want every step recorded as an append-only Audit Event, so that I can reconstruct what the agent and tools did.
25. As an Admin, I want Audit Events to be immutable (no update/delete for the app role), so that the trail cannot be rewritten.
26. As an Admin, I want an Operations queue with live status, so that I can watch requested → reserved → applied → reconciled (or failed/flagged).
27. As an Admin, I want each Operation linked to its agent run and PRISM trace URL, so that I can open the Steps proof in one click.
28. As a presenter, I want to choose Agent Version v1 or v2 when seeding Operations, so that I can show failure then fix on the same inputs.
29. As a presenter, I want v1 to be able to skip reservation and apply without full Program Rules checks, so that concurrency and over-cap failures are reproducible.
30. As a presenter, I want v2 to require ordered checks (rules → balance → eligibility → reserve → apply → Reconcile → audit) and flag on any failure, so that the Root Cause fix is visible.
31. As a presenter, I want to seed sample data (one Program, multiple Teams, Grants, caps, plus an expired Program), so that demos reset cleanly.
32. As a presenter, I want to snapshot and reset the Issuer Ledger, so that v1 and v2 batches start from the same state.
33. As a presenter, I want to run a seeded batch of Operations (happy path, concurrent drain, over-cap, ineligible, expired, duplicate Idempotency Key), so that PRISM clusters form with enough volume.
34. As the agent worker, I want to claim requested Operations safely under concurrency, so that two workers never process the same Operation.
35. As the agent worker, I want all model calls to go through the Nicrron API, so that we meet the hackathon requirement without treating that API as the Issuer Ledger.
36. As the agent worker, I want tools to own arithmetic and ledger writes, so that the model cannot invent balances (ADR-0002).
37. As the system, I want a stale Reservation to expire and release if apply never completes, so that a crashed worker leaves no applied Credit and no permanent hold.
38. As an Admin, I want a Reconciliation page that highlights per-Team mismatches in red, so that judges can see money failure at a glance.
39. As an Admin, I want an Audit log filterable by Operation, so that I can walk one move end-to-end.
40. As a judge, I want the pitch to state clearly that the Issuer Ledger is simulated, so that we do not claim real Nicrron Credit movement (ADR-0001).
41. As a Team participant, I want Request to mean Reallocate from Program Pool to my Team, so that self-serve does not require naming another Team.
42. As an Admin, I want Return to credit the Program Pool Team, so that surplus is pooled explicitly rather than auto-clawed back.
43. As the system, I want a correctly Flagged Operation to count as a successful agent run for demo scoring, while ledger mismatch or tool errors count as failed, so that PRISM proofs match the TDD.
44. As a developer, I want tests at the Operation-processing seam against the Issuer Ledger, so that money safety is proven without coupling to UI or PRISM.
45. As a developer, I want the same seed scenarios used in tests and in the demo batch, so that demo behavior matches the test plan.

## Implementation Decisions

- **Product shape**: CreditPool is Issuer-side allocation software for one Issuer, one Program, team/pool reallocation only — not a public exchange, order book, cash withdrawal, stablecoin settlement, or key pool (ADR-0001 scope).
- **Components**: Lovable UI + Supabase (schema, Realtime, Postgres functions) + Python agent worker on a laptop; worker is the only component that talks to Nicrron API and PRISM.
- **Authoritative money store**: Simulated `issuer_ledger` is the source of truth; app tables record Operations, Reservations, Audit Events, and agent runs and Reconcile against the ledger (ADR-0001).
- **Program Pool**: Modeled as a special Team in the Program so Return, Request, and Admin Reallocate share one balance machinery.
- **Self-serve vs Admin**: Teams may Return → Pool and Request ← Pool; Admin may also Reallocate Team → Team in the same Program.
- **Operation types**: At minimum `return` and `reallocate` (Request is a Reallocate with `from_team` = Program Pool).
- **Operation status flow**: `requested` → `reserved` → `applied` → `reconciled`, or `failed` / `flagged` from any step; claiming uses a conditional update to `processing` so workers do not double-claim.
- **Agent tools** (external behavior of the worker): get_operation, get_program_rules, get_team_balance, check_eligibility, reserve_funds, apply_reallocation, reconcile, write_audit_event, flag_for_admin.
- **v1 vs v2 apply**: Separate apply paths — v1 writes ledger entries with weak/no checks; v2 requires active Reservation, enforces Program Rules inside a single Postgres function with row locks on affected Teams, and is idempotent on the Operation's Idempotency Key.
- **Schema (logical)**: programs, teams, issuer_ledger, operations, reservations, allocation_events (insert-only), agent_runs — as specified in the CreditPool TDD.
- **Nicrron API**: OpenAI-compatible chat with tool calls for agent reasoning only; pin a model for recorded v1/v2 comparison; `nicrron/auto` acceptable for volume runs.
- **PRISM**: One session per Operation; root span carries `agent_version` and `operation_id`; tool and model child spans; submit trajectory after each run; alerts on failed `reconcile` or `apply_reallocation`.
- **UI screens**: Program console, Team wallet, Operations queue, Reconciliation, Audit log, Demo control (seed, snapshot/reset, v1/v2 toggle, run batch).
- **Test seam (agreed)**: Operation processing against the Issuer Ledger — create Operation, run reserve→apply→Reconcile, assert status, balances, Program Rules, Audit Events, idempotency. UI, Nicrron API, and PRISM are out of that seam.

## Testing Decisions

- **Good tests** assert external behavior only: Operation outcomes, Issuer Ledger / Available Balance, Program Rules violations, Idempotency Key semantics, Audit Event append-only behavior, Reservation expiry after crash/timeout. Do not assert prompt text, internal helper structure, or PRISM span shape unless required for a thin adapter smoke test.
- **Primary module under test**: the money path invoked by Operation processing (v2 reserve + apply + Reconcile against the simulated Issuer Ledger). Prefer calling the same tools/RPCs the agent would call, without requiring a live LLM for the money suite.
- **Scenarios that must pass on v2** (and fail or mis-apply on v1 where noted): concurrent drain; duplicate Idempotency Key; over-cap; ineligible recipient; expired Program; cross-Program claim; model timeout / worker crash between reserve and apply (Reservation releases, no apply); Audit Event immutability; prompt-injection cannot bypass Postgres rules.
- **Prior art**: none in-repo yet (greenfield). Establish this suite as the canonical money tests; demo seed batch should reuse the same scenario definitions.
- **Out of money-suite**: full Lovable UI clicks, live PRISM workspace quotas, and Nicrron model variance — cover those with manual demo checklist / smoke only.

## Out of Scope

- Public seller listings, order books, cash withdrawals, stablecoin settlement, cross-provider conversion.
- Pooled user API keys or handling participant prompts as a proxy.
- Moving real Nicrron Credit balances or real personal data.
- Cross-Program claims or multi-Issuer tenancy beyond one simulated Issuer.
- Licensed closed-model resale or open-weight GPU capacity exchange (research Phase 1/3 paths).
- Money-transmitter licensing and production payments.

## Further Notes

- Domain language lives in `GLOSSARY.md`; follow it in UI copy and issue titles.
- ADR-0001: simulated Issuer Ledger; say so in the demo.
- ADR-0002: ledger/tools enforce money, not the model.
- Hackathon constraints: NicrronNinoo for model calls; PRISM Sessions (6+ steps), Alert, Root Cause via v1 then v2 on the same seed batch; Credit expiry pressure on event day for real Nicrron API usage budget.
- Open implementation questions (not domain): exact PRISM failure counting, alert filter granularity, Nicrron function-calling support for the pinned model — confirm with mentors at the event.
