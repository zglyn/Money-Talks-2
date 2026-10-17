"""Operation processing seam over the simulated Issuer Ledger (ADR-0001, ADR-0002).

Every public function is one write transaction. Failed rule checks do not raise: they
Flag the Operation, release its Reservation, append an Audit Event, commit, and return a
failed `StepResult`, so a refusal never leaves half-applied Credit.
"""

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterator

DEFAULT_RESERVATION_TTL_SECONDS = 300.0


class Status(StrEnum):
    REQUESTED = "requested"
    RESERVED = "reserved"
    APPLIED = "applied"
    RECONCILED = "reconciled"
    FAILED = "failed"
    FLAGGED = "flagged"


_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.REQUESTED: frozenset({Status.RESERVED, Status.APPLIED, Status.FLAGGED, Status.FAILED}),
    Status.RESERVED: frozenset({Status.REQUESTED, Status.APPLIED, Status.FLAGGED, Status.FAILED}),
    Status.APPLIED: frozenset({Status.RECONCILED, Status.FAILED}),
    Status.RECONCILED: frozenset(),
    Status.FAILED: frozenset(),
    Status.FLAGGED: frozenset({Status.REQUESTED, Status.FAILED}),
}
_DONE = frozenset({Status.APPLIED, Status.RECONCILED})


class RuleViolation(Exception):
    """Raised by Grant, which has no Operation to Flag."""

    def __init__(self, rule: str, detail: str = "") -> None:
        super().__init__(f"{rule}: {detail}" if detail else rule)
        self.rule = rule


@dataclass(frozen=True)
class StepResult:
    ok: bool
    rule: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class LedgerFingerprint:
    """Checksum over one Program's balances and the Operations that authorize them."""

    program_id: int
    digest: str
    ok: bool


@dataclass(frozen=True)
class ReconcileResult:
    ok: bool
    mismatches: tuple[str, ...] = ()
    fingerprint: LedgerFingerprint | None = None


@dataclass(frozen=True)
class Operation:
    id: int
    program_id: int
    type: str
    from_team_id: int
    to_team_id: int
    amount_cents: int
    idempotency_key: str
    status: Status
    agent_version: str
    flag_reason: str | None


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _load(conn: sqlite3.Connection, operation_id: int) -> Operation:
    row = conn.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
    if row is None:
        raise LookupError(f"operation {operation_id} not found")
    return Operation(
        id=row["id"],
        program_id=row["program_id"],
        type=row["type"],
        from_team_id=row["from_team_id"],
        to_team_id=row["to_team_id"],
        amount_cents=row["amount_cents"],
        idempotency_key=row["idempotency_key"],
        status=Status(row["status"]),
        agent_version=row["agent_version"],
        flag_reason=row["flag_reason"],
    )


def get_operation(conn: sqlite3.Connection, operation_id: int) -> Operation:
    return _load(conn, operation_id)


def audit_steps(conn: sqlite3.Connection, operation_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT step FROM allocation_events WHERE operation_id = ? ORDER BY id", (operation_id,)
    )
    return [r["step"] for r in rows]


def _audit(
    conn: sqlite3.Connection, operation_id: int, actor: str, step: str, detail: str, now: float
) -> None:
    conn.execute(
        "INSERT INTO allocation_events (operation_id, actor, step, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (operation_id, actor, step, detail, now),
    )


def _audit_fingerprint(
    conn: sqlite3.Connection, operation_id: int, fingerprint: LedgerFingerprint, now: float
) -> None:
    """Record OK or drift once per digest. A retry of the same Reconcile appends nothing."""
    step = "fingerprint_ok" if fingerprint.ok else "fingerprint_drift"
    last = conn.execute(
        "SELECT step, detail FROM allocation_events"
        " WHERE operation_id = ? AND step IN ('fingerprint_ok', 'fingerprint_drift')"
        " ORDER BY id DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    if last is not None and last["step"] == step and last["detail"] == fingerprint.digest:
        return
    _audit(conn, operation_id, "system", step, fingerprint.digest, now)


def _set_status(
    conn: sqlite3.Connection, op: Operation, to: Status, now: float, flag_reason: str | None = None
) -> None:
    if to not in _TRANSITIONS[op.status]:
        raise ValueError(f"operation {op.id}: illegal transition {op.status} -> {to}")
    conn.execute(
        "UPDATE operations SET status = ?, flag_reason = COALESCE(?, flag_reason), updated_at = ?"
        " WHERE id = ?",
        (to.value, flag_reason, now, op.id),
    )


def _team(conn: sqlite3.Connection, team_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()


def _program(conn: sqlite3.Connection, program_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM programs WHERE id = ?", (program_id,)).fetchone()
    if row is None:
        raise LookupError(f"program {program_id} not found")
    return row


def ledger_balance(conn: sqlite3.Connection, team_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM issuer_ledger WHERE team_id = ?", (team_id,)
    ).fetchone()
    return row["s"]


def _reserved(conn: sqlite3.Connection, team_id: int, now: float) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM reservations"
        " WHERE team_id = ? AND status = 'active' AND expires_at > ?",
        (team_id, now),
    ).fetchone()
    return row["s"]


def available_balance(conn: sqlite3.Connection, team_id: int, now: float | None = None) -> int:
    """Issuer Ledger balance minus active (unexpired) Reservations."""
    return ledger_balance(conn, team_id) - _reserved(conn, team_id, _now(now))


def program_total(conn: sqlite3.Connection, program_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM issuer_ledger WHERE program_id = ?",
        (program_id,),
    ).fetchone()
    return row["s"]


def pool_team_id(conn: sqlite3.Connection, program_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM teams WHERE program_id = ? AND is_pool = 1", (program_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"program {program_id} has no pool team")
    return row["id"]


def create_program(
    conn: sqlite3.Connection,
    name: str,
    total_budget_cents: int,
    per_team_cap_cents: int,
    expires_at: float,
    returns_allowed: bool = True,
    now: float | None = None,
) -> int:
    """Create a Program and its Program Pool Team."""
    t = _now(now)
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO programs (name, total_budget_cents, per_team_cap_cents, expires_at,"
            " returns_allowed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, total_budget_cents, per_team_cap_cents, expires_at, int(returns_allowed), t),
        )
        program_id = cur.lastrowid
        conn.execute(
            "INSERT INTO teams (program_id, name, eligible, is_pool) VALUES (?, 'Program Pool', 1, 1)",
            (program_id,),
        )
    return program_id


def add_team(
    conn: sqlite3.Connection, program_id: int, name: str, eligible: bool = True
) -> int:
    with _tx(conn):
        _program(conn, program_id)
        cur = conn.execute(
            "INSERT INTO teams (program_id, name, eligible, is_pool) VALUES (?, ?, ?, 0)",
            (program_id, name, int(eligible)),
        )
    return cur.lastrowid


def grant(
    conn: sqlite3.Connection, team_id: int, amount_cents: int, now: float | None = None
) -> int:
    """Credit a Team on the Issuer Ledger. Refuses to exceed Program budget or per-Team cap."""
    t = _now(now)
    if amount_cents <= 0:
        raise ValueError("grant amount must be positive")
    with _tx(conn):
        team = _team(conn, team_id)
        if team is None:
            raise LookupError(f"team {team_id} not found")
        program = _program(conn, team["program_id"])
        if program_total(conn, program["id"]) + amount_cents > program["total_budget_cents"]:
            raise RuleViolation("budget", "grant would exceed Program total budget")
        if (
            not team["is_pool"]
            and ledger_balance(conn, team_id) + amount_cents > program["per_team_cap_cents"]
        ):
            raise RuleViolation("cap", "grant would exceed per-Team cap")
        cur = conn.execute(
            "INSERT INTO issuer_ledger (program_id, team_id, amount_cents, kind, created_at)"
            " VALUES (?, ?, ?, 'grant', ?)",
            (program["id"], team_id, amount_cents, t),
        )
    return cur.lastrowid


def create_operation(
    conn: sqlite3.Connection,
    *,
    program_id: int,
    type: str,
    from_team_id: int,
    to_team_id: int | None = None,
    amount_cents: int,
    idempotency_key: str,
    agent_version: str = "v2",
    now: float | None = None,
) -> Operation:
    """Create an Operation, or return the existing one for a repeated Idempotency Key.

    A `return` always targets the Program Pool. A Request is a `reallocate` whose
    `from_team_id` is the Program Pool. Team ids are not checked against `program_id`
    here: Program Rules refuse cross-Program references at apply.
    """
    t = _now(now)
    with _tx(conn):
        existing = conn.execute(
            "SELECT id FROM operations WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            return _load(conn, existing["id"])
        if type not in ("return", "reallocate"):
            raise ValueError(f"unknown operation type {type!r}")
        if amount_cents <= 0:
            raise ValueError("amount must be positive")
        if type == "return":
            pool = pool_team_id(conn, program_id)
            if to_team_id not in (None, pool):
                raise ValueError("a return must target the Program Pool")
            to_team_id = pool
        elif to_team_id is None:
            raise ValueError("a reallocate needs a to_team_id")
        if from_team_id == to_team_id:
            raise ValueError("from and to team must differ")
        cur = conn.execute(
            "INSERT INTO operations (program_id, type, from_team_id, to_team_id, amount_cents,"
            " idempotency_key, status, agent_version, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?)",
            (
                program_id,
                type,
                from_team_id,
                to_team_id,
                amount_cents,
                idempotency_key,
                agent_version,
                t,
                t,
            ),
        )
        _audit(conn, cur.lastrowid, "system", "created", type, t)
        return _load(conn, cur.lastrowid)


def create_return(
    conn: sqlite3.Connection,
    program_id: int,
    team_id: int,
    amount_cents: int,
    idempotency_key: str,
    agent_version: str = "v2",
    now: float | None = None,
) -> Operation:
    return create_operation(
        conn,
        program_id=program_id,
        type="return",
        from_team_id=team_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
        agent_version=agent_version,
        now=now,
    )


def create_request(
    conn: sqlite3.Connection,
    program_id: int,
    team_id: int,
    amount_cents: int,
    idempotency_key: str,
    agent_version: str = "v2",
    now: float | None = None,
) -> Operation:
    return create_operation(
        conn,
        program_id=program_id,
        type="reallocate",
        from_team_id=pool_team_id(conn, program_id),
        to_team_id=team_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
        agent_version=agent_version,
        now=now,
    )


def _release_reservations(conn: sqlite3.Connection, operation_id: int) -> None:
    conn.execute(
        "UPDATE reservations SET status = 'released' WHERE operation_id = ? AND status = 'active'",
        (operation_id,),
    )


def _flag(
    conn: sqlite3.Connection, op: Operation, rule: str, detail: str, actor: str, now: float
) -> StepResult:
    _release_reservations(conn, op.id)
    _set_status(conn, op, Status.FLAGGED, now, flag_reason=rule)
    _audit(conn, op.id, actor, "flagged", f"{rule}: {detail}" if detail else rule, now)
    return StepResult(False, rule, detail)


def flag_for_admin(
    conn: sqlite3.Connection, operation_id: int, reason: str, actor: str = "agent",
    now: float | None = None,
) -> StepResult:
    t = _now(now)
    with _tx(conn):
        op = _load(conn, operation_id)
        if op.status not in (Status.REQUESTED, Status.RESERVED):
            return StepResult(False, "bad_status", f"cannot flag a {op.status} operation")
        _flag(conn, op, reason, "", actor, t)
    return StepResult(True)


def write_audit_event(
    conn: sqlite3.Connection, operation_id: int, actor: str, step: str, detail: str = "",
    now: float | None = None,
) -> None:
    t = _now(now)
    with _tx(conn):
        _load(conn, operation_id)
        _audit(conn, operation_id, actor, step, detail, t)


def _expire_stale(conn: sqlite3.Connection, now: float) -> int:
    stale = conn.execute(
        "SELECT id, operation_id FROM reservations WHERE status = 'active' AND expires_at <= ?",
        (now,),
    ).fetchall()
    for r in stale:
        conn.execute("UPDATE reservations SET status = 'expired' WHERE id = ?", (r["id"],))
        op = _load(conn, r["operation_id"])
        if op.status is Status.RESERVED:
            _set_status(conn, op, Status.REQUESTED, now)
        _audit(conn, op.id, "system", "reservation_expired", "", now)
    return len(stale)


def release_expired_reservations(conn: sqlite3.Connection, now: float | None = None) -> int:
    """Release stale Reservations (a worker crashed or timed out before apply)."""
    t = _now(now)
    with _tx(conn):
        return _expire_stale(conn, t)


def reserve_funds(
    conn: sqlite3.Connection,
    operation_id: int,
    ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
    now: float | None = None,
) -> StepResult:
    """Hold the Operation's amount on the source Team. Flags when Available Balance is short."""
    t = _now(now)
    with _tx(conn):
        _expire_stale(conn, t)
        op = _load(conn, operation_id)
        if op.status is Status.RESERVED:
            return StepResult(True)
        if op.status is not Status.REQUESTED:
            return StepResult(False, "bad_status", f"cannot reserve a {op.status} operation")
        available = available_balance(conn, op.from_team_id, t)
        if available < op.amount_cents:
            return _flag(
                conn, op, "insufficient_available",
                f"available {available} < {op.amount_cents}", "system", t,
            )
        conn.execute(
            "INSERT INTO reservations (operation_id, team_id, amount_cents, status, expires_at,"
            " created_at) VALUES (?, ?, ?, 'active', ?, ?)",
            (op.id, op.from_team_id, op.amount_cents, t + ttl_seconds, t),
        )
        _set_status(conn, op, Status.RESERVED, t)
        _audit(conn, op.id, "system", "reserved", str(op.amount_cents), t)
    return StepResult(True)


def _write_transfer(conn: sqlite3.Connection, op: Operation, now: float) -> None:
    for team_id, amount, kind in (
        (op.from_team_id, -op.amount_cents, "transfer_out"),
        (op.to_team_id, op.amount_cents, "transfer_in"),
    ):
        team = _team(conn, team_id)
        conn.execute(
            "INSERT INTO issuer_ledger (program_id, team_id, amount_cents, kind, operation_id,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (team["program_id"], team_id, amount, kind, op.id, now),
        )


def apply_reallocation_v1(
    conn: sqlite3.Connection, operation_id: int, now: float | None = None
) -> StepResult:
    """UNSAFE. Writes the transfer with no Reservation and no Program Rules checks."""
    t = _now(now)
    with _tx(conn):
        op = _load(conn, operation_id)
        if op.status in _DONE:
            return StepResult(True)
        if op.status not in (Status.REQUESTED, Status.RESERVED):
            return StepResult(False, "bad_status", f"cannot apply a {op.status} operation")
        _write_transfer(conn, op, t)
        _release_reservations(conn, op.id)
        _set_status(conn, op, Status.APPLIED, t)
        _audit(conn, op.id, "agent-v1", "applied", "v1 unchecked apply", t)
    return StepResult(True)


def _program_rule_violation(
    conn: sqlite3.Connection, op: Operation, now: float
) -> tuple[str, str] | None:
    """First failing Program Rule in the order rules, balance, eligibility, reservation."""
    src, dst = _team(conn, op.from_team_id), _team(conn, op.to_team_id)
    if src is None or dst is None:
        return "unknown_team", "team does not exist"
    if src["program_id"] != op.program_id or dst["program_id"] != op.program_id:
        return "program_mismatch", "team belongs to a different Program"
    program = _program(conn, op.program_id)
    if op.type == "return" and not program["returns_allowed"]:
        return "returns_disabled", "Program does not allow Returns"
    if now >= program["expires_at"]:
        return "expired", "Program has expired"
    if ledger_balance(conn, src["id"]) < op.amount_cents:
        return "insufficient_balance", "source ledger balance too low"
    if not dst["eligible"]:
        return "ineligible", "recipient Team is not eligible"
    if (
        not dst["is_pool"]
        and ledger_balance(conn, dst["id"]) + op.amount_cents > program["per_team_cap_cents"]
    ):
        return "cap", "recipient would exceed per-Team cap"
    if program_total(conn, op.program_id) > program["total_budget_cents"]:
        return "budget", "Program total exceeds budget"
    held = conn.execute(
        "SELECT 1 FROM reservations WHERE operation_id = ? AND status = 'active'"
        " AND team_id = ? AND amount_cents = ? AND expires_at > ?",
        (op.id, op.from_team_id, op.amount_cents, now),
    ).fetchone()
    if held is None:
        return "no_reservation", "no active Reservation for this Operation"
    return None


def apply_reallocation_v2(
    conn: sqlite3.Connection, operation_id: int, now: float | None = None
) -> StepResult:
    """Apply the transfer only under an active Reservation and passing every Program Rule.

    Idempotent: an already applied Operation returns ok without a second ledger move.
    """
    t = _now(now)
    with _tx(conn):
        op = _load(conn, operation_id)
        if op.status in _DONE:
            return StepResult(True)
        if op.status not in (Status.REQUESTED, Status.RESERVED):
            return StepResult(False, "bad_status", f"cannot apply a {op.status} operation")
        violation = _program_rule_violation(conn, op, t)
        if violation is not None:
            return _flag(conn, op, violation[0], violation[1], "system", t)
        _write_transfer(conn, op, t)
        conn.execute(
            "UPDATE reservations SET status = 'applied' WHERE operation_id = ? AND status = 'active'",
            (op.id,),
        )
        _set_status(conn, op, Status.APPLIED, t)
        _audit(conn, op.id, "system", "applied", str(op.amount_cents), t)
    return StepResult(True)


def _mismatches(conn: sqlite3.Connection, op: Operation, now: float) -> list[str]:
    found: list[str] = []
    rows = conn.execute(
        "SELECT team_id, amount_cents FROM issuer_ledger WHERE operation_id = ?", (op.id,)
    ).fetchall()
    expected = sorted([(op.from_team_id, -op.amount_cents), (op.to_team_id, op.amount_cents)])
    if sorted((r["team_id"], r["amount_cents"]) for r in rows) != expected:
        found.append("ledger_pair")
    src, dst = _team(conn, op.from_team_id), _team(conn, op.to_team_id)
    if {src["program_id"], dst["program_id"]} != {op.program_id}:
        found.append("cross_program")
    for team in (src, dst):
        if ledger_balance(conn, team["id"]) < 0:
            found.append("negative_balance")
        if available_balance(conn, team["id"], now) < 0:
            found.append("negative_available")
    program = _program(conn, dst["program_id"])
    if not dst["is_pool"] and ledger_balance(conn, dst["id"]) > program["per_team_cap_cents"]:
        found.append("over_cap")
    for program_id in {src["program_id"], dst["program_id"]}:
        if program_total(conn, program_id) > _program(conn, program_id)["total_budget_cents"]:
            found.append("over_budget")
    if conn.execute(
        "SELECT 1 FROM reservations WHERE operation_id = ? AND status = 'active'", (op.id,)
    ).fetchone():
        found.append("dangling_reservation")
    return list(dict.fromkeys(found))


def ledger_fingerprint(conn: sqlite3.Connection, program_id: int) -> LedgerFingerprint:
    """Checksum of Program and Team balances plus applied or reconciled Operation identity.

    `ok` is false when those balances disagree with Grants plus that identity.
    A failed Reconcile drops the Operation out of the identity, so leftover
    Issuer Ledger rows read as drift.
    """
    _program(conn, program_id)
    teams = conn.execute(
        "SELECT id FROM teams WHERE program_id = ? ORDER BY id", (program_id,)
    ).fetchall()
    actual = [(row["id"], ledger_balance(conn, row["id"])) for row in teams]
    expected = {team_id: 0 for team_id, _ in actual}
    for row in conn.execute(
        "SELECT team_id, amount_cents FROM issuer_ledger WHERE program_id = ? AND kind = 'grant'",
        (program_id,),
    ):
        if row["team_id"] in expected:
            expected[row["team_id"]] += row["amount_cents"]
    ops = conn.execute(
        "SELECT id, type, from_team_id, to_team_id, amount_cents FROM operations"
        " WHERE program_id = ? AND status IN ('applied', 'reconciled') ORDER BY id",
        (program_id,),
    ).fetchall()
    team_ids = set(expected)
    for op in ops:
        if op["from_team_id"] in team_ids:
            expected[op["from_team_id"]] -= op["amount_cents"]
        if op["to_team_id"] in team_ids:
            expected[op["to_team_id"]] += op["amount_cents"]
    total = program_total(conn, program_id)
    ok = total == sum(balance for _, balance in actual) and all(
        balance == expected[team_id] for team_id, balance in actual
    )
    lines = [f"program {program_id} {total}"]
    lines.extend(f"team {team_id} {balance}" for team_id, balance in actual)
    lines.extend(
        f"operation {op['id']} {op['type']} {op['from_team_id']} {op['to_team_id']} {op['amount_cents']}"
        for op in ops
    )
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    return LedgerFingerprint(program_id, digest, ok)


def reconcile(
    conn: sqlite3.Connection, operation_id: int, now: float | None = None
) -> ReconcileResult:
    """Verify the Issuer Ledger, the Operation, and Reservations agree.

    Ledger Fingerprint drift is a money failure. It fails an applied Operation.
    A repeat on an already reconciled Operation reports drift and leaves the status.
    """
    t = _now(now)
    with _tx(conn):
        op = _load(conn, operation_id)
        if op.status is Status.RECONCILED:
            fp = ledger_fingerprint(conn, op.program_id)
            if fp.ok:
                return ReconcileResult(True, fingerprint=fp)
            _audit_fingerprint(conn, op.id, fp, t)
            return ReconcileResult(False, ("fingerprint_drift",), fp)
        if op.status is not Status.APPLIED:
            return ReconcileResult(
                False, ("not_applied",), ledger_fingerprint(conn, op.program_id)
            )
        found = _mismatches(conn, op, t)
        if not ledger_fingerprint(conn, op.program_id).ok and "fingerprint_drift" not in found:
            found.append("fingerprint_drift")
        if found:
            _set_status(conn, op, Status.FAILED, t, flag_reason="reconcile_mismatch")
            _audit(conn, op.id, "system", "reconcile_failed", ",".join(found), t)
            fp = ledger_fingerprint(conn, op.program_id)
            _audit_fingerprint(conn, op.id, fp, t)
            return ReconcileResult(False, tuple(found), fp)
        _set_status(conn, op, Status.RECONCILED, t)
        fp = ledger_fingerprint(conn, op.program_id)
        _audit_fingerprint(conn, op.id, fp, t)
        _audit(conn, op.id, "system", "reconciled", "", t)
    return ReconcileResult(True, fingerprint=fp)


def process_operation(
    conn: sqlite3.Connection, operation_id: int, now: float | None = None
) -> Operation:
    """Run one Operation through its Agent Version's money path: v2 reserve, apply, reconcile."""
    op = _load(conn, operation_id)
    if op.agent_version == "v1":
        applied = apply_reallocation_v1(conn, operation_id, now=now)
    else:
        applied = reserve_funds(conn, operation_id, now=now)
        if applied.ok:
            applied = apply_reallocation_v2(conn, operation_id, now=now)
    if applied.ok:
        reconcile(conn, operation_id, now=now)
    return _load(conn, operation_id)


def resolve_flagged(
    conn: sqlite3.Connection,
    operation_id: int,
    action: str,
    reason: str = "",
    now: float | None = None,
) -> Operation:
    """Admin resolution of a Flagged Operation: `retry` requeues it; `reject` fails it."""
    if action not in ("retry", "reject"):
        raise ValueError("action must be retry or reject")
    t = _now(now)
    with _tx(conn):
        op = _load(conn, operation_id)
        if op.status is not Status.FLAGGED:
            raise ValueError(f"operation {op.id} is {op.status}, not flagged")
        detail = reason.strip() or ("requeued by Admin" if action == "retry" else "rejected by Admin")
        if action == "retry":
            conn.execute(
                "UPDATE operations SET status = ?, flag_reason = NULL, claimed_by = NULL,"
                " claimed_at = NULL, updated_at = ? WHERE id = ?",
                (Status.REQUESTED.value, t, op.id),
            )
            _audit(conn, op.id, "admin", "admin_retry", detail, t)
        else:
            _set_status(conn, op, Status.FAILED, t, flag_reason="admin_reject")
            _audit(conn, op.id, "admin", "admin_reject", detail, t)
    return _load(conn, operation_id)


def active_reservations(
    conn: sqlite3.Connection, team_id: int, now: float | None = None
) -> list[sqlite3.Row]:
    """Active Reservations holding Credit on a Team (for wallet countdown UI)."""
    t = _now(now)
    return conn.execute(
        "SELECT r.*, o.type AS operation_type, o.status AS operation_status"
        " FROM reservations r JOIN operations o ON o.id = r.operation_id"
        " WHERE r.team_id = ? AND r.status = 'active' AND r.expires_at > ?"
        " ORDER BY r.expires_at",
        (team_id, t),
    ).fetchall()
