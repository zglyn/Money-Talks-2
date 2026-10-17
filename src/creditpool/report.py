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
class ProgramPulse:
    """Time to expiry plus surplus and dry Teams for one Program.

    A non-pool Team is surplus when Available Balance is above zero, and dry when
    it is at or below zero. Available Balance already subtracts active Reservations.
    The Program Pool is neither.
    """

    seconds_to_expiry: int
    surplus_teams: tuple[int, ...]
    dry_teams: tuple[int, ...]
    return_href: str
    request_href: str
    offer_href: str = "/trades"

    @property
    def surplus_count(self) -> int:
        return len(self.surplus_teams)

    @property
    def dry_count(self) -> int:
        return len(self.dry_teams)

    @property
    def expiry_label(self) -> str:
        return _expiry_label(self.seconds_to_expiry)


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
    pulse: ProgramPulse
    teams: list[TeamRow] = field(default_factory=list)

    @property
    def remaining_cents(self) -> int:
        return self.total_budget_cents - self.used_cents


def _expiry_label(seconds: int) -> str:
    if seconds <= 0:
        return "Expired"
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _wallet_href(team_id: int | None, anchor: str) -> str:
    if team_id is None:
        return f"/wallet#{anchor}"
    return f"/wallet?team_id={team_id}#{anchor}"


def program_pulse(teams: list[TeamRow], expires_at: float, now: float) -> ProgramPulse:
    members = [t for t in teams if not t.is_pool]
    surplus = [t for t in members if t.available_cents > 0]
    dry = [t for t in members if t.available_cents <= 0]
    return_team = max(surplus, key=lambda t: (t.available_cents, -t.id), default=None)
    request_team = min(dry, key=lambda t: (t.available_cents, t.id), default=None)
    return_id = None if return_team is None else return_team.id
    request_id = None if request_team is None else request_team.id
    return ProgramPulse(
        seconds_to_expiry=max(0, int(expires_at - now)),
        surplus_teams=tuple(t.id for t in surplus),
        dry_teams=tuple(t.id for t in dry),
        return_href=_wallet_href(return_id, "return"),
        request_href=_wallet_href(request_id, "request"),
    )


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
            pulse=program_pulse(teams, p["expires_at"], t),
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
        " (SELECT outcome FROM agent_runs r WHERE r.operation_id = o.id ORDER BY r.id DESC LIMIT 1)"
        "   AS run_outcome,"
        " (SELECT prism_trace_id FROM agent_runs r WHERE r.operation_id = o.id ORDER BY r.id DESC LIMIT 1)"
        "   AS trace_id"
        " FROM operations o JOIN teams s ON s.id = o.from_team_id JOIN teams d ON d.id = o.to_team_id"
        " JOIN programs p ON p.id = o.program_id"
    )
    if status:
        return conn.execute(sql + " WHERE o.status = ? ORDER BY o.id DESC", (status,)).fetchall()
    return conn.execute(sql + " ORDER BY o.id DESC").fetchall()


def audit_events(conn: sqlite3.Connection, operation_id: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM allocation_events"
    if operation_id is None:
        return conn.execute(sql + " ORDER BY id DESC LIMIT 500").fetchall()
    return conn.execute(sql + " WHERE operation_id = ? ORDER BY id", (operation_id,)).fetchall()


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM operations GROUP BY status")
    return {r["status"]: r["n"] for r in rows}
