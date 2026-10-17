## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues (via `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## PRISM tracing (do not remove)

This project sends traces to PRISM. Env vars: `PRISMTRACE_API_KEY`,
`PRISMTRACE_PROJECT_ID`, `PRISMTRACE_HOST`.

Tracing is currently wired at: `src/creditpool/prism.py`, `src/creditpool/worker.py` (`run_agent`), `src/creditpool/env.py`, `src/creditpool/app.py` (loads `.env`).

**Standing rule.** Whenever you add or change an agent, chain, graph, tool,
retriever, or any entry point that calls a model, wire it to PRISM before you
finish. Unwired code is invisible in the dashboard. If you are unsure whether
something is covered, assume it is not and wire it.

**Operate from the repo.** Auth with `X-PRISMtrace-Key` (not Bearer). Env:
`PRISMTRACE_HOST`, `PRISMTRACE_PROJECT_ID`, `PRISMTRACE_ORG_ID`, `PRISMTRACE_API_KEY`.
Start with `GET /api/setup-doctor`. Prefer free reads (`/api/traces`, `/api/intelligence`,
clusters, remediation list, alerts). Never call paid endpoints (cluster analyze,
trace-analysis backfill, narrative, generate-fix) without stating the credit cost
and getting an explicit yes. Check `GET /api/credits/balance` before multi-spend plans.
