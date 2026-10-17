"""Demo data: a MONEY TALKS 2 Program, an expired one, snapshot/reset, and the scenario batch.

The same scenario definitions feed tests and the demo (TDD story 45).
"""

import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass

from creditpool import money

PROGRAM_NAME = "MONEY TALKS 2"
EXPIRED_PROGRAM_NAME = "Spring Credits (expired)"
TEAM_COUNT = 12
GRANT_CENTS = 26_000
CAP_CENTS = 60_000
INELIGIBLE_TEAM = "Team 12"


@dataclass(frozen=True)
class DemoWorld:
    program_id: int
    pool_id: int
    teams: dict[str, int]
    expired_program_id: int
    expired_pool_id: int
    expired_teams: dict[str, int]


@dataclass(frozen=True)
class BatchResult:
    operation_ids: list[int]
    submitted: int

    @property
    def duplicates_collapsed(self) -> int:
        return self.submitted - len(self.operation_ids)


def _program_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM programs WHERE name = ?", (name,)).fetchone()
    return None if row is None else row["id"]


def _teams(conn: sqlite3.Connection, program_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT id, name FROM teams WHERE program_id = ? AND is_pool = 0 ORDER BY id", (program_id,)
    )
    return {r["name"]: r["id"] for r in rows}


def demo_world(conn: sqlite3.Connection) -> DemoWorld:
    """Look up the seeded demo Programs. Raises LookupError before `seed_demo`."""
    program_id = _program_id(conn, PROGRAM_NAME)
    expired_id = _program_id(conn, EXPIRED_PROGRAM_NAME)
    if program_id is None or expired_id is None:
        raise LookupError("demo not seeded")
    return DemoWorld(
        program_id, money.pool_team_id(conn, program_id), _teams(conn, program_id),
        expired_id, money.pool_team_id(conn, expired_id), _teams(conn, expired_id),
    )


def seed_demo(conn: sqlite3.Connection, now: float | None = None) -> DemoWorld:
    """Create both Programs, their Teams and Grants. Safe to call twice."""
    t = time.time() if now is None else now
    if _program_id(conn, PROGRAM_NAME) is None:
        program_id = money.create_program(
            conn, PROGRAM_NAME, TEAM_COUNT * GRANT_CENTS, CAP_CENTS, t + 7 * 86_400, now=t
        )
        for n in range(1, TEAM_COUNT + 1):
            name = f"Team {n:02d}"
            team_id = money.add_team(conn, program_id, name, eligible=name != INELIGIBLE_TEAM)
            money.grant(conn, team_id, GRANT_CENTS, now=t)
    if _program_id(conn, EXPIRED_PROGRAM_NAME) is None:
        expired_id = money.create_program(
            conn, EXPIRED_PROGRAM_NAME, 30_000, 20_000, t - 86_400, now=t - 30 * 86_400
        )
        for name in ("Legacy A", "Legacy B", "Legacy C"):
            money.grant(conn, money.add_team(conn, expired_id, name), 10_000, now=t)
    return demo_world(conn)


def snapshot(conn: sqlite3.Connection) -> bytes:
    """Serialize the whole database (ledger, operations, audit trail, agent runs)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "snapshot.db")
        copy = sqlite3.connect(path)
        try:
            conn.backup(copy)
            copy.execute("PRAGMA journal_mode = DELETE")
        finally:
            copy.close()
        with open(path, "rb") as f:
            return f.read()


def restore(conn: sqlite3.Connection, snap: bytes) -> None:
    """Replace the database contents with a snapshot, bypassing the append-only audit triggers."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "snapshot.db")
        with open(path, "wb") as f:
            f.write(snap)
        source = sqlite3.connect(path)
        try:
            source.backup(conn)
        finally:
            source.close()


def insert_batch(conn: sqlite3.Connection, agent_version: str) -> BatchResult:
    """Insert the scenario Operations (about 60) for one Agent Version, in processing order.

    Keys carry the Agent Version, so re-inserting a batch collapses onto the same Operations.
    """
    world = demo_world(conn)
    ids: list[int] = []
    submitted = 0

    def submit(program_id: int, kind: str, team_id: int, amount: int, key: str, to: int | None = None):
        nonlocal submitted
        key = f"demo-{agent_version}-{key}"
        factory = {"return": money.create_return, "request": money.create_request}
        submitted += 1
        if kind == "move":
            op = money.create_operation(
                conn, program_id=program_id, type="reallocate", from_team_id=team_id,
                to_team_id=to, amount_cents=amount, idempotency_key=key,
                agent_version=agent_version,
            )
        else:
            op = factory[kind](conn, program_id, team_id, amount, key, agent_version)
        if op.id not in ids:
            ids.append(op.id)
        return op

    def t(n: int) -> int:
        return world.teams[f"Team {n:02d}"]

    pid = world.program_id

    for n in range(1, 11):
        submit(pid, "return", t(n), 8_000, f"normal-return-{n:02d}")
    for n in range(1, 8):
        for rep in (1, 2):
            submit(pid, "request", t(n), 2_500, f"normal-request-{n:02d}-{rep}")
    for i, (src, dst) in enumerate([(7, 9), (9, 7), (6, 9), (9, 6)], start=1):
        submit(pid, "move", t(src), 1_500, f"normal-admin-{i}", to=t(dst))

    for i, (src, amount, dsts) in enumerate([(11, 20_000, (2, 3)), (10, 15_000, (4, 5)), (8, 15_000, (1, 2))], start=1):
        for side, dst in zip("ab", dsts):
            submit(pid, "move", t(src), amount, f"drain-{i}{side}", to=t(dst))

    for n in range(1, 7):
        submit(pid, "request", t(n), 40_000, f"over-cap-{n}")

    for i in range(1, 4):
        submit(pid, "request", t(12), 3_000, f"ineligible-request-{i}")
        submit(pid, "move", t(i), 3_000, f"ineligible-admin-{i}", to=t(12))

    legacy = list(world.expired_teams.values())
    for i, team_id in enumerate(legacy, start=1):
        submit(world.expired_program_id, "return", team_id, 1_000, f"expired-return-{i}")
        submit(world.expired_program_id, "request", team_id, 1_000, f"expired-request-{i}")

    for n in range(1, 7):
        for _ in (1, 2):
            submit(pid, "request", t(n), 1_000, f"duplicate-{n}")

    return BatchResult(ids, submitted)
