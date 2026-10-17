# CreditPool

Issuer-controlled reallocation of unused hackathon program Credit between Teams. Teams Return unused Credit to a Program Pool and Request Credit from it. An agent sequences each Operation, and tools plus the ledger enforce the money rules. Domain terms are in `GLOSSARY.md`.

**The Issuer Ledger is simulated.** No real Nicrron balances move (ADR-0001). The agent does not decide money either, the tools do (ADR-0002).

The hackathon build replaces the planned Lovable + Supabase stack with FastAPI + SQLite, so the whole demo runs from one process and one file. The Postgres schema and functions remain in `supabase/migrations/` for a Supabase deployment.

## Install

Python 3.11 or newer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

Optional extras:

```powershell
pip install -e ".[nicrron]"   # real Nicrron model calls
pip install -e ".[prism]"     # PRISM live tracing SDK
```

## Secrets and environment

1. Copy `.env.example` to `.env` (gitignored).
2. Put real `PRISMTRACE_*` / `NICRRON_*` values only in `.env`. Never commit them. Never paste them into chat if you can avoid it.
3. The app and worker call `env.load()` so `.env` is picked up from the process working directory.

PRISM wiring lives in `src/creditpool/prism.py` and is used from `run_agent` in `src/creditpool/worker.py`. Without `PRISMTRACE_*` set, the worker still runs and keeps a local trace id.

## Test

```powershell
python -m pytest
```

`tests/test_money.py` covers the money core. `tests/test_worker.py` covers claiming and the agent worker. `tests/test_demo.py` covers the seed scenarios and the UI.

## Run the UI

```powershell
uvicorn creditpool.app:app --reload
```

Open http://127.0.0.1:8000. The database is the file named by `CREDITPOOL_DB` (default `creditpool.db` in the working directory). A snapshot is saved next to it as `<db>.snapshot`.

## Demo flow

1. Open **Demo control** and press **Seed demo**. This creates 12 Teams with a $260 Grant each, a $600 cap, a Program Pool, an expired second Program, and a snapshot.
2. Set the Agent Version to **v1** and press **Insert and process batch**. About 60 Operations run: normal, concurrent drain pairs, over-cap, ineligible, expired, and duplicate Idempotency Keys.
3. Open **Reconciliation**. v1 applies without a Reservation or full Program Rules, so Teams show red mismatches and Operations fail Reconcile.
4. Back on Demo control, press **Reset to snapshot**, set **v2**, and run the same batch.
5. Reconciliation is clean. Risky moves are Flagged instead, and every Operation shows 6 or more tool steps in its agent run.

Other pages: **Program console** (budget used and remaining), **Team wallet** (Return and Request forms), **Operations** (queue, claim owner, agent run and PRISM trace id), **Audit log** (filter by Operation number).

## Worker

`creditpool.worker` runs one Operation through an OpenAI-compatible chat loop with nine tools: `get_operation`, `get_program_rules`, `get_team_balance`, `check_eligibility`, `reserve_funds`, `apply_reallocation`, `reconcile`, `write_audit_event`, `flag_for_admin`.

```python
from creditpool import db
from creditpool.worker import claim_and_process_loop

conn = db.connect("creditpool.db")
runs = claim_and_process_loop(conn, worker_id="worker-1")
```

- **Claiming.** `claim_operations` sets `claimed_by` and `claimed_at` on requested Operations in one `UPDATE`, so two workers never get the same row. Money statuses are unchanged.
- **Model.** `default_llm()` picks the backend. Without `NICRRON_API_KEY`, that is `MockLLM` (no network). With the key and `pip install -e ".[nicrron]"`, it uses `NicrronLLM` against `https://api.nicrron.ai/v1` (override with `NICRRON_BASE_URL`, `NICRRON_MODEL`). Demo and queue buttons call `default_llm()`. The Nicrron API is for reasoning only; it does not move Program Credit. Live Nicrron and live PRISM export are not covered by the automated tests.
- **PRISM.** With `PRISMTRACE_API_KEY`, `PRISMTRACE_PROJECT_ID`, and `PRISMTRACE_HOST` set (and `pip install -e ".[prism]"`), each Operation runs inside `prismtrace.session`, emits `trace_llm` for model turns, and `submit_trajectory` for tool steps. Without those env vars, `PrismRecorder` keeps a local UUID only. Optional `PRISMTRACE_EXPORTER=module:function` still works as a custom hook.
