"""Read-only views over the Issuer Ledger for the demo UI."""

import sqlite3
import time
from dataclasses import dataclass, field

from creditpool import money


@dataclass(frozen=True)
class TeamRow:
    id: int
    name: str
    is_pool: bool
    eligible: bool
    ledger_cents: int
    available_cents: int
    mismatches: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProgramView:
    id: int
    name: str
    total_budget_cents: int
    used_cents: int
    per_team_cap_cents: int
    expires_at: float
    expired: bool
    returns_allowed: bool
    teams: list[TeamRow] = field(default_factory=list)

    @property
    def remaining_cents(self) -> int:
        return self.total_budget_cents - self.used_cents


def _operation_net(conn: sqlite3.Connection, team_id: int) -> int:
    """Net transfer the Operations table says this Team should hold (applied or reconciled)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN to_team_id = ? THEN amount_cents ELSE 0 END), 0)"
        " - COALESCE(SUM(CASE WHEN from_team_id = ? THEN amount_cents ELSE 0 END), 0) AS n"
        " FROM operations WHERE status IN ('applied', 'reconciled')",
        (team_id, team_id),
    ).fetchone()
    return row["n"]


def _ledger_net(conn: sqlite3.Connection, team_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS n FROM issuer_ledger"
        " WHERE team_id = ? AND kind <> 'grant'",
        (team_id,),
    ).fetchone()
    return row["n"]


def team_mismatches(conn: sqlite3.Connection, team: sqlite3.Row, cap: int) -> tuple[str, ...]:
    found: list[str] = []
    balance = money.ledger_balance(conn, team["id"])
    if balance < 0:
        found.append("negative_balance")
    if not team["is_pool"] and balance > cap:
        found.append("over_cap")
    if _ledger_net(conn, team["id"]) != _operation_net(conn, team["id"]):
        found.append("ledger_vs_operations")
    return tuple(found)


def programs(conn: sqlite3.Connection, now: float | None = None) -> list[ProgramView]:
    t = time.time() if now is None else now
    views: list[ProgramView] = []
    for p in conn.execute("SELECT * FROM programs ORDER BY id").fetchall():
        teams = [
            TeamRow(
                id=r["id"], name=r["name"], is_pool=bool(r["is_pool"]), eligible=bool(r["eligible"]),
                ledger_cents=money.ledger_balance(conn, r["id"]),
                available_cents=money.available_balance(conn, r["id"], t),
                mismatches=team_mismatches(conn, r, p["per_team_cap_cents"]),
            )
            for r in conn.execute(
                "SELECT * FROM teams WHERE program_id = ? ORDER BY is_pool DESC, id", (p["id"],)
            )
        ]
        views.append(ProgramView(
            id=p["id"], name=p["name"], total_budget_cents=p["total_budget_cents"],
            used_cents=money.program_total(conn, p["id"]),
            per_team_cap_cents=p["per_team_cap_cents"], expires_at=p["expires_at"],
            expired=t >= p["expires_at"], returns_allowed=bool(p["returns_allowed"]), teams=teams,
        ))
    return views


def program_mismatches(view: ProgramView) -> tuple[str, ...]:
    return ("over_budget",) if view.used_cents > view.total_budget_cents else ()


def failed_reconciles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT o.id, o.type, o.amount_cents, e.detail FROM operations o"
        " LEFT JOIN allocation_events e ON e.operation_id = o.id AND e.step = 'reconcile_failed'"
        " WHERE o.status = 'failed' ORDER BY o.id"
    ).fetchall()


def operations(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT o.*, s.name AS from_name, d.name AS to_name, p.name AS program_name,"
        " tr.id AS trade_id,"
        " (SELECT outcome FROM agent_runs r WHERE r.operation_id = o.id ORDER BY r.id DESC LIMIT 1)"
        "   AS run_outcome,"
        " (SELECT prism_trace_id FROM agent_runs r WHERE r.operation_id = o.id ORDER BY r.id DESC LIMIT 1)"
        "   AS trace_id"
        " FROM operations o JOIN teams s ON s.id = o.from_team_id JOIN teams d ON d.id = o.to_team_id"
        " JOIN programs p ON p.id = o.program_id"
        " LEFT JOIN trades tr ON tr.operation_id = o.id"
    )
    if status:
        return conn.execute(sql + " WHERE o.status = ? ORDER BY o.id DESC", (status,)).fetchall()
    return conn.execute(sql + " ORDER BY o.id DESC").fetchall()


def audit_events(conn: sqlite3.Connection, operation_id: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM allocation_events"
    if operation_id is None:
        return conn.execute(sql + " ORDER BY id DESC LIMIT 500").fetchall()
    return conn.execute(sql + " WHERE operation_id = ? ORDER BY id", (operation_id,)).fetchall()


def offers(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT o.*, t.name AS team_name, p.name AS program_name"
        " FROM offers o JOIN teams t ON t.id = o.team_id JOIN programs p ON p.id = o.program_id"
    )
    if status:
        return conn.execute(sql + " WHERE o.status = ? ORDER BY o.id DESC", (status,)).fetchall()
    return conn.execute(sql + " ORDER BY o.id DESC").fetchall()


def trades(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT tr.*, s.name AS from_name, d.name AS to_name, p.name AS program_name,"
        " op.status AS operation_status, op.type AS operation_type"
        " FROM trades tr"
        " JOIN teams s ON s.id = tr.from_team_id JOIN teams d ON d.id = tr.to_team_id"
        " JOIN programs p ON p.id = tr.program_id"
        " JOIN operations op ON op.id = tr.operation_id"
        " ORDER BY tr.id DESC"
    ).fetchall()


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM operations GROUP BY status")
    return {r["status"]: r["n"] for r in rows}
