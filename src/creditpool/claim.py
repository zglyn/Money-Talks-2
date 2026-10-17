"""Worker claiming for requested Operations.

A claim is `claimed_by`/`claimed_at` on the Operation, not a new Status, so the money
state machine is untouched. One UPDATE statement selects and claims atomically under
SQLite's write lock, so two workers can never claim the same row.
"""

import sqlite3
import time


def claim_operations(
    conn: sqlite3.Connection, limit: int, worker_id: str, now: float | None = None
) -> list[int]:
    """Claim up to `limit` unclaimed requested Operations, oldest first. Returns their ids."""
    t = time.time() if now is None else now
    rows = conn.execute(
        "UPDATE operations SET claimed_by = ?, claimed_at = ?"
        " WHERE claimed_by IS NULL AND id IN ("
        "   SELECT id FROM operations WHERE status = 'requested' AND claimed_by IS NULL"
        "   ORDER BY id LIMIT ?)"
        " RETURNING id",
        (worker_id, t, limit),
    ).fetchall()
    return sorted(r["id"] for r in rows)


def release_stale_claims(
    conn: sqlite3.Connection, older_than_seconds: float, now: float | None = None
) -> int:
    """Free claims on still-requested Operations whose worker died before finishing."""
    t = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE operations SET claimed_by = NULL, claimed_at = NULL"
        " WHERE status = 'requested' AND claimed_by IS NOT NULL AND claimed_at <= ?",
        (t - older_than_seconds,),
    )
    return cur.rowcount
