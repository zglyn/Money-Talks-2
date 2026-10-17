"""FastAPI demo for CreditPool. One SQLite file, path from CREDITPOOL_DB (default creditpool.db)."""

import csv
import io
import os
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from creditpool import db, env, money, presets, report, seed
from creditpool.worker import claim_and_process_loop, default_llm

env.load()

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["usd"] = lambda cents: f"${cents / 100:,.2f}" if cents >= 0 else f"-${-cents / 100:,.2f}"
TEMPLATES.env.filters["when"] = lambda ts: (
    datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
)
TEMPLATES.env.filters["secs_left"] = lambda expires, now: max(0, int(expires - now))


def _cents(amount: str) -> int:
    try:
        cents = int((Decimal(amount) * 100).to_integral_exact())
    except (InvalidOperation, ValueError):
        raise HTTPException(400, f"not a dollar amount: {amount!r}")
    if cents <= 0:
        raise HTTPException(400, "amount must be positive")
    return cents


def _hours(value: str) -> float:
    try:
        hours = float(value)
    except ValueError:
        raise HTTPException(400, "expiry offset must be a number of hours") from None
    if hours <= 0:
        raise HTTPException(400, "expiry offset must be positive")
    return hours


def _optional_program_id(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise HTTPException(400, "program_id must be an integer") from None


def _back(path: str, msg: str) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}msg={quote(msg)}", status_code=303)


def _alerts_from_runs(runs) -> list[dict]:
    """Alert-worthy tool failures: reconcile / apply_reallocation errors, plus failed runs."""
    alerts = []
    seen: set[tuple[int, str]] = set()
    for r in runs:
        for span in r.prism.spans:
            name = span.get("name") or ""
            if name not in ("reconcile", "apply_reallocation"):
                continue
            out = span.get("output") or {}
            bad = bool(span.get("error")) or out.get("ok") is False or "error" in out
            if not bad:
                continue
            key = (r.operation_id, name)
            if key in seen:
                continue
            seen.add(key)
            alerts.append({
                "tool": name,
                "operation_id": r.operation_id,
                "agent_version": r.agent_version,
                "detail": out.get("rule") or out.get("error") or r.error_code or r.outcome,
            })
        if r.outcome == "failed" and (r.operation_id, "reconcile") not in seen:
            seen.add((r.operation_id, "reconcile"))
            alerts.append({
                "tool": "reconcile",
                "operation_id": r.operation_id,
                "agent_version": r.agent_version,
                "detail": r.error_code or "failed",
            })
    return alerts


def _run_batch(conn: sqlite3.Connection, version: str, worker_id: str) -> dict:
    batch = seed.insert_batch(conn, version)
    runs = claim_and_process_loop(conn, worker_id, default_llm())
    return {
        "agent_version": version,
        "submitted": batch.submitted,
        "operations": len(batch.operation_ids),
        "duplicates_collapsed": batch.duplicates_collapsed,
        "outcomes": dict(Counter(r.outcome for r in runs)),
        "steps_min": min((len(r.steps) for r in runs), default=0),
        "alerts": _alerts_from_runs(runs),
        "failed": sum(1 for r in runs if not r.ok),
        "mismatch_teams": sum(
            1 for p in report.programs(conn) for t in p.teams if t.mismatches
        ),
    }


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("CREDITPOOL_DB", "creditpool.db")
    snapshot_path = Path(f"{path}.snapshot")
    app = FastAPI(title="CreditPool")
    app.state.agent_version = "v2"  # demo UI is safe-agent only; v1 remains in tests/money core
    app.state.last_run = None
    app.state.pinned_model = os.environ.get("NICRRON_MODEL") or "nicrron/auto"

    boot = db.connect(path)
    db.init_schema(boot)
    boot.close()

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Depends(get_conn)

    def page(request: Request, name: str, **context):
        return TEMPLATES.TemplateResponse(request, name, {
            "agent_version": request.app.state.agent_version,
            "msg": request.query_params.get("msg"),
            "pinned_model": request.app.state.pinned_model,
            **context,
        })

    def _configure_target(conn: sqlite3.Connection, program_id: int | None):
        views = report.programs(conn)
        if program_id is None:
            return None, views
        selected = next((p for p in views if p.id == program_id), None)
        if selected is None:
            raise HTTPException(404, "Program not found")
        return selected, views

    @app.get("/configure")
    def configure(request: Request, program_id: int | None = None, conn: sqlite3.Connection = Conn):
        selected, views = _configure_target(conn, program_id)
        now = time.time()
        hours = ""
        if selected is not None:
            remaining = (selected.expires_at - now) / 3600
            hours = f"{remaining:.2f}".rstrip("0").rstrip(".")
        return page(
            request, "configure.html",
            program=selected, programs=views, policy_presets=presets.POLICY_PRESETS,
            stances=list(presets.EligibilityStance), stance_labels=presets.STANCE_LABELS,
            grants_exist=bool(selected and money.program_has_grants(conn, selected.id)),
            grants_lock=money.GRANTS_LOCK_DETAIL,
            expiry_hours=hours,
        )

    @app.post("/configure/preset")
    def configure_preset(
        name: str = Form(""), preset: str = Form(), program_id: str = Form(""),
        conn: sqlite3.Connection = Conn,
    ):
        try:
            presets.require(preset)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        existing = _optional_program_id(program_id)
        try:
            if existing is None:
                program_name = name.strip()
                if not program_name:
                    raise HTTPException(400, "Program name is required")
                program_id_int = money.create_program_from_preset(conn, program_name, preset)
            else:
                money.apply_policy_preset(conn, existing, preset)
                program_id_int = existing
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except money.RuleViolation as exc:
            return _back(f"/configure?program_id={existing}", exc.detail)
        return _back(f"/configure?program_id={program_id_int}", f"Policy Preset {preset} applied")

    @app.post("/configure/rules")
    def configure_rules(
        name: str = Form(""), program_id: str = Form(""),
        budget: str = Form(), cap: str = Form(), eligibility_stance: str = Form(),
        returns_allowed: str = Form(), expiry_offset_hours: str = Form(),
        conn: sqlite3.Connection = Conn,
    ):
        if returns_allowed not in ("yes", "no"):
            raise HTTPException(400, "Returns allowed must be yes or no")
        try:
            stance = presets.EligibilityStance(eligibility_stance)
        except ValueError:
            raise HTTPException(400, "unknown eligibility stance") from None
        rules = presets.ProgramRules(
            total_budget_cents=_cents(budget),
            per_team_cap_cents=_cents(cap),
            eligibility_stance=stance,
            returns_allowed=returns_allowed == "yes",
            expires_at=time.time() + _hours(expiry_offset_hours) * 3600,
        )
        existing = _optional_program_id(program_id)
        try:
            if existing is None:
                program_name = name.strip()
                if not program_name:
                    raise HTTPException(400, "Program name is required")
                program_id_int = money.create_program(
                    conn, program_name, rules.total_budget_cents, rules.per_team_cap_cents,
                    rules.expires_at, returns_allowed=rules.returns_allowed,
                    eligibility_stance=rules.eligibility_stance.value,
                )
            else:
                money.update_program_rules(conn, existing, rules)
                program_id_int = existing
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except money.RuleViolation as exc:
            return _back(f"/configure?program_id={existing}", exc.detail)
        return _back(f"/configure?program_id={program_id_int}", "Program Rules saved")

    @app.post("/configure/team")
    def configure_team(
        program_id: int = Form(), name: str = Form(), eligible: str = Form(),
        conn: sqlite3.Connection = Conn,
    ):
        team_name = name.strip()
        if not team_name:
            raise HTTPException(400, "Team name is required")
        if eligible not in ("yes", "no"):
            raise HTTPException(400, "eligible must be yes or no")
        _configure_target(conn, program_id)
        try:
            money.add_team(conn, program_id, team_name, eligible == "yes")
        except sqlite3.IntegrityError:
            return _back(f"/configure?program_id={program_id}", f"Team {team_name} already exists")
        return _back(f"/configure?program_id={program_id}", f"Team {team_name} added")

    @app.post("/configure/grant")
    def configure_grant(
        program_id: int = Form(), team_id: int = Form(), amount: str = Form(),
        conn: sqlite3.Connection = Conn,
    ):
        team = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if team is None or team["program_id"] != program_id or team["is_pool"]:
            raise HTTPException(400, "pick a Team in this Program")
        try:
            money.grant(conn, team_id, _cents(amount))
        except money.RuleViolation as exc:
            return _back(f"/configure?program_id={program_id}", exc.detail)
        return _back(f"/configure?program_id={program_id}", "Grant recorded")

    @app.get("/")
    def console(request: Request, conn: sqlite3.Connection = Conn):
        views = report.programs(conn)
        return page(
            request, "console.html", programs=views, report=report,
            key=uuid.uuid4().hex[:12],
            flagged=report.operations(conn, "flagged"),
        )

    @app.post("/admin/reallocate")
    def admin_reallocate(
        program_id: int = Form(), from_team_id: int = Form(), to_team_id: int = Form(),
        amount: str = Form(), key: str = Form(""),
        conn: sqlite3.Connection = Conn,
    ):
        if from_team_id == to_team_id:
            raise HTTPException(400, "pick two different Teams")
        op = money.create_operation(
            conn, program_id=program_id, type="reallocate",
            from_team_id=from_team_id, to_team_id=to_team_id,
            amount_cents=_cents(amount),
            idempotency_key=f"ui-admin-{key or uuid.uuid4().hex}",
            agent_version=app.state.agent_version,
        )
        return _back("/operations", f"Admin Reallocate queued as Operation #{op.id}")

    @app.post("/admin/resolve")
    def admin_resolve(
        operation_id: int = Form(), action: str = Form(), reason: str = Form(""),
        conn: sqlite3.Connection = Conn,
    ):
        try:
            op = money.resolve_flagged(conn, operation_id, action, reason)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _back("/operations", f"Operation #{op.id} → {op.status.value}")

    @app.get("/wallet")
    def wallet(request: Request, team_id: int | None = None, conn: sqlite3.Connection = Conn):
        import time
        now = time.time()
        views = report.programs(conn)
        teams = [(p, t) for p in views for t in p.teams if not t.is_pool]
        selected = next(((p, t) for p, t in teams if t.id == team_id), teams[0] if teams else None)
        recent = []
        holds = []
        if selected:
            recent = [
                o for o in report.operations(conn)
                if selected[1].id in (o["from_team_id"], o["to_team_id"])
            ][:10]
            holds = money.active_reservations(conn, selected[1].id, now)
        return page(
            request, "wallet.html", teams=teams, selected=selected, recent=recent,
            key=uuid.uuid4().hex[:12], holds=holds, now=now,
        )

    def _submit(conn: sqlite3.Connection, kind: str, team_id: int, amount: str, key: str):
        team = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if team is None or team["is_pool"]:
            raise HTTPException(400, "pick a Team")
        make = money.create_return if kind == "return" else money.create_request
        return make(
            conn, team["program_id"], team_id, _cents(amount),
            f"ui-{kind}-{key or uuid.uuid4().hex}", app.state.agent_version,
        )

    @app.post("/wallet/return")
    def wallet_return(
        team_id: int = Form(), amount: str = Form(), key: str = Form(""),
        conn: sqlite3.Connection = Conn,
    ):
        op = _submit(conn, "return", team_id, amount, key)
        return _back("/operations", f"Return queued as Operation #{op.id}")

    @app.post("/wallet/request")
    def wallet_request(
        team_id: int = Form(), amount: str = Form(), key: str = Form(""),
        conn: sqlite3.Connection = Conn,
    ):
        op = _submit(conn, "request", team_id, amount, key)
        return _back("/operations", f"Request queued as Operation #{op.id}")

    @app.get("/operations")
    def operations(request: Request, status: str | None = None, conn: sqlite3.Connection = Conn):
        return page(
            request, "operations.html", operations=report.operations(conn, status or None),
            counts=report.status_counts(conn), status=status or "",
            statuses=[s.value for s in money.Status],
            flagged=report.operations(conn, "flagged"),
        )

    @app.post("/operations/process")
    def process_queue(conn: sqlite3.Connection = Conn):
        runs = claim_and_process_loop(conn, "ui-worker", default_llm())
        return _back("/operations", f"Processed {len(runs)} Operation(s)")

    @app.get("/reconciliation")
    def reconciliation(request: Request, conn: sqlite3.Connection = Conn):
        views = report.programs(conn)
        return page(
            request, "reconciliation.html", programs=views, report=report,
            failed=report.failed_reconciles(conn),
            alerts=(request.app.state.last_run or {}).get("alerts") or [],
        )

    @app.get("/audit")
    def audit(request: Request, operation_id: int | None = None, conn: sqlite3.Connection = Conn):
        return page(
            request, "audit.html", events=report.audit_events(conn, operation_id),
            operation_id=operation_id,
        )

    @app.get("/audit.csv")
    def audit_csv(operation_id: int | None = None, conn: sqlite3.Connection = Conn):
        events = report.audit_events(conn, operation_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "id", "operation_id", "program_id", "program_name", "created_at", "actor", "step", "detail",
        ])
        for e in events:
            w.writerow([
                e["id"], e["operation_id"] or "", e["program_id"] or "", e["program_name"],
                datetime.fromtimestamp(e["created_at"], timezone.utc).isoformat(),
                e["actor"], e["step"], e["detail"],
            ])
        buf.seek(0)
        name = f"audit-op-{operation_id}.csv" if operation_id else "audit.csv"
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/demo")
    def demo(request: Request, conn: sqlite3.Connection = Conn):
        return page(
            request, "demo.html", counts=report.status_counts(conn),
            seeded=conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0] > 0,
            has_snapshot=snapshot_path.exists(), last_run=request.app.state.last_run,
        )

    @app.post("/demo/seed")
    def demo_seed(conn: sqlite3.Connection = Conn):
        seed.seed_demo(conn)
        snapshot_path.write_bytes(seed.snapshot(conn))
        return _back("/demo", "Seeded 12 Teams and an expired Program; snapshot saved")

    @app.post("/demo/snapshot")
    def demo_snapshot(conn: sqlite3.Connection = Conn):
        snapshot_path.write_bytes(seed.snapshot(conn))
        return _back("/demo", "Snapshot saved")

    @app.post("/demo/reset")
    def demo_reset(request: Request, conn: sqlite3.Connection = Conn):
        if not snapshot_path.exists():
            raise HTTPException(400, "no snapshot yet; seed or snapshot first")
        seed.restore(conn, snapshot_path.read_bytes())
        request.app.state.last_run = None
        return _back("/demo", "Ledger restored from snapshot")

    @app.post("/demo/model")
    def demo_model(request: Request, model: str = Form()):
        model = (model or "").strip() or "nicrron/auto"
        request.app.state.pinned_model = model
        os.environ["NICRRON_MODEL"] = model
        return _back("/demo", f"Pinned model {model}")

    @app.post("/demo/run")
    def demo_run(request: Request, conn: sqlite3.Connection = Conn):
        request.app.state.agent_version = "v2"
        try:
            summary = _run_batch(conn, "v2", "demo-worker")
        except LookupError:
            raise HTTPException(400, "seed the demo first")
        request.app.state.last_run = summary
        return _back(
            "/reconciliation",
            f"Batch done: {summary['operations']} processed, "
            f"{summary['outcomes'].get('flagged', 0)} flagged, {summary['mismatch_teams']} mismatches",
        )

    @app.post("/demo/run-full")
    def demo_run_full(request: Request, conn: sqlite3.Connection = Conn):
        """One-click safe demo: seed → snapshot → v2 batch."""
        seed.seed_demo(conn)
        snapshot_path.write_bytes(seed.snapshot(conn))
        request.app.state.agent_version = "v2"
        summary = _run_batch(conn, "v2", "demo-full")
        request.app.state.last_run = summary
        return _back(
            "/demo",
            f"Demo ready: {summary['operations']} Operations, "
            f"{summary['outcomes'].get('flagged', 0)} flagged, ledger clean.",
        )

    return app


def __getattr__(name: str) -> FastAPI:
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
