"""SQLite schema for the simulated Issuer Ledger and the tables that record Operations.

Money is integer cents (USD). `allocation_events` is insert-only: triggers abort any
UPDATE or DELETE, and `money.py` exposes no API that attempts one.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    total_budget_cents INTEGER NOT NULL CHECK (total_budget_cents > 0),
    per_team_cap_cents INTEGER NOT NULL CHECK (per_team_cap_cents > 0),
    expires_at REAL NOT NULL,
    returns_allowed INTEGER NOT NULL DEFAULT 1 CHECK (returns_allowed IN (0, 1)),
    eligibility_stance TEXT NOT NULL DEFAULT 'open' CHECK (eligibility_stance IN ('listed', 'open')),
    policy_preset TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES programs (id),
    name TEXT NOT NULL,
    eligible INTEGER NOT NULL DEFAULT 1 CHECK (eligible IN (0, 1)),
    is_pool INTEGER NOT NULL DEFAULT 0 CHECK (is_pool IN (0, 1)),
    UNIQUE (program_id, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pool_per_program ON teams (program_id) WHERE is_pool = 1;

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES programs (id),
    type TEXT NOT NULL CHECK (type IN ('return', 'reallocate')),
    from_team_id INTEGER NOT NULL,
    to_team_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (
        status IN ('requested', 'reserved', 'applied', 'reconciled', 'failed', 'flagged')
    ),
    agent_version TEXT NOT NULL DEFAULT 'v2' CHECK (agent_version IN ('v1', 'v2')),
    flag_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    claimed_by TEXT,
    claimed_at REAL
);

CREATE TABLE IF NOT EXISTS issuer_ledger (
    id INTEGER PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES programs (id),
    team_id INTEGER NOT NULL REFERENCES teams (id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents <> 0),
    kind TEXT NOT NULL CHECK (kind IN ('grant', 'transfer_out', 'transfer_in')),
    operation_id INTEGER REFERENCES operations (id),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS issuer_ledger_team ON issuer_ledger (team_id);

CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY,
    operation_id INTEGER NOT NULL REFERENCES operations (id),
    team_id INTEGER NOT NULL REFERENCES teams (id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'applied', 'released', 'expired')),
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_reservation_per_operation
    ON reservations (operation_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS allocation_events (
    id INTEGER PRIMARY KEY,
    operation_id INTEGER NOT NULL REFERENCES operations (id),
    actor TEXT NOT NULL,
    step TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS allocation_events_no_update
BEFORE UPDATE ON allocation_events
BEGIN SELECT RAISE(ABORT, 'allocation_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS allocation_events_no_delete
BEFORE DELETE ON allocation_events
BEGIN SELECT RAISE(ABORT, 'allocation_events is append-only'); END;

CREATE TABLE IF NOT EXISTS program_audit_events (
    id INTEGER PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES programs (id),
    actor TEXT NOT NULL,
    step TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS program_audit_events_no_update
BEFORE UPDATE ON program_audit_events
BEGIN SELECT RAISE(ABORT, 'program_audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS program_audit_events_no_delete
BEFORE DELETE ON program_audit_events
BEGIN SELECT RAISE(ABORT, 'program_audit_events is append-only'); END;

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY,
    operation_id INTEGER NOT NULL REFERENCES operations (id),
    agent_version TEXT NOT NULL CHECK (agent_version IN ('v1', 'v2')),
    outcome TEXT NOT NULL,
    tool_calls TEXT NOT NULL DEFAULT '[]',
    error_code TEXT,
    prism_trace_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_runs_operation ON agent_runs (operation_id);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection in autocommit mode; money.py opens explicit write transactions."""
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables; upgrade databases created before claim columns and the new agent_runs."""
    runs = _columns(conn, "agent_runs")
    if runs and "outcome" not in runs:
        conn.execute("DROP TABLE agent_runs")
    cols = _columns(conn, "operations")
    if cols and "claimed_by" not in cols:
        conn.execute("ALTER TABLE operations ADD COLUMN claimed_by TEXT")
        conn.execute("ALTER TABLE operations ADD COLUMN claimed_at REAL")
    programs = _columns(conn, "programs")
    if programs and "eligibility_stance" not in programs:
        conn.execute(
            "ALTER TABLE programs ADD COLUMN eligibility_stance TEXT NOT NULL DEFAULT 'open'"
        )
    if programs and "policy_preset" not in programs:
        conn.execute("ALTER TABLE programs ADD COLUMN policy_preset TEXT")
    conn.executescript(SCHEMA)
