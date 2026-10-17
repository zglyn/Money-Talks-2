"""Behavior tests at the Operation-processing seam against the simulated Issuer Ledger."""

import sqlite3
import threading
import time
from dataclasses import dataclass

import pytest

from creditpool import db, money
from creditpool.money import Status

BUDGET = 10_000
CAP = 5_000
NOW = time.time()


@dataclass
class World:
    path: str
    conn: sqlite3.Connection
    program_id: int
    pool: int
    a: int
    b: int


@pytest.fixture
def world(tmp_path):
    path = str(tmp_path / "creditpool.db")
    conn = db.connect(path)
    db.init_schema(conn)
    program_id = money.create_program(conn, "Hackathon", BUDGET, CAP, NOW + 3600, now=NOW)
    a = money.add_team(conn, program_id, "Alpha")
    b = money.add_team(conn, program_id, "Beta")
    yield World(path, conn, program_id, money.pool_team_id(conn, program_id), a, b)
    conn.close()


def ledger_total(conn):
    return conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM issuer_ledger").fetchone()[0]


def move(w, key, src, dst, amount, version="v2", now=NOW):
    op = money.create_operation(
        w.conn, program_id=w.program_id, type="reallocate", from_team_id=src, to_team_id=dst,
        amount_cents=amount, idempotency_key=key, agent_version=version, now=now,
    )
    return money.process_operation(w.conn, op.id, now=now)


def test_grants_show_in_balances_and_pool_starts_empty(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    money.grant(world.conn, world.b, 1_000, now=NOW)

    assert money.ledger_balance(world.conn, world.a) == 3_000
    assert money.available_balance(world.conn, world.a, NOW) == 3_000
    assert money.ledger_balance(world.conn, world.pool) == 0
    assert money.program_total(world.conn, world.program_id) == 4_000


def test_grant_refused_over_budget_or_cap(world):
    money.grant(world.conn, world.a, CAP, now=NOW)
    with pytest.raises(money.RuleViolation) as cap:
        money.grant(world.conn, world.a, 1, now=NOW)
    assert cap.value.rule == "cap"

    money.grant(world.conn, world.b, CAP, now=NOW)
    with pytest.raises(money.RuleViolation) as budget:
        money.grant(world.conn, world.pool, BUDGET - 2 * CAP + 1, now=NOW)
    assert budget.value.rule == "budget"
    assert money.program_total(world.conn, world.program_id) == 2 * CAP


def test_return_moves_credit_to_pool_and_reconciles(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    op = money.create_return(world.conn, world.program_id, world.a, 1_000, "ret-1", now=NOW)

    done = money.process_operation(world.conn, op.id, now=NOW)

    assert done.status is Status.RECONCILED
    assert money.ledger_balance(world.conn, world.a) == 2_000
    assert money.ledger_balance(world.conn, world.pool) == 1_000
    assert money.available_balance(world.conn, world.a, NOW) == 2_000
    assert money.audit_steps(world.conn, op.id) == ["created", "reserved", "applied", "reconciled"]


def test_active_reservation_reduces_available_balance(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    op = money.create_return(world.conn, world.program_id, world.a, 1_000, "ret-1", now=NOW)

    assert money.reserve_funds(world.conn, op.id, now=NOW).ok

    assert money.ledger_balance(world.conn, world.a) == 3_000
    assert money.available_balance(world.conn, world.a, NOW) == 2_000


def test_request_draws_from_pool_to_team(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    money.process_operation(
        world.conn,
        money.create_return(world.conn, world.program_id, world.a, 2_000, "ret-1", now=NOW).id,
        now=NOW,
    )
    op = money.create_request(world.conn, world.program_id, world.b, 500, "req-1", now=NOW)

    assert op.from_team_id == world.pool
    done = money.process_operation(world.conn, op.id, now=NOW)

    assert done.status is Status.RECONCILED
    assert money.ledger_balance(world.conn, world.b) == 500
    assert money.ledger_balance(world.conn, world.pool) == 1_500


def test_request_beyond_pool_balance_is_flagged(world):
    op = money.create_request(world.conn, world.program_id, world.b, 500, "req-1", now=NOW)

    done = money.process_operation(world.conn, op.id, now=NOW)

    assert done.status is Status.FLAGGED
    assert done.flag_reason == "insufficient_available"
    assert money.ledger_balance(world.conn, world.b) == 0


def test_admin_reallocates_team_to_team(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)

    done = move(world, "adm-1", world.a, world.b, 1_200)

    assert done.status is Status.RECONCILED
    assert money.ledger_balance(world.conn, world.a) == 1_800
    assert money.ledger_balance(world.conn, world.b) == 1_200


def assert_refused_without_ledger_change(w, done, rule, before):
    assert done.status is Status.FLAGGED
    assert done.flag_reason == rule
    assert ledger_total(w.conn) == before
    assert money.ledger_balance(w.conn, w.a) == 3_000
    assert money.available_balance(w.conn, w.a, NOW) == 3_000
    assert "applied" not in money.audit_steps(w.conn, done.id)
    assert money.audit_steps(w.conn, done.id)[-1] == "flagged"


def test_over_cap_recipient_is_flagged(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    money.grant(world.conn, world.b, 4_500, now=NOW)

    done = move(world, "cap-1", world.a, world.b, 1_000)

    assert done.status is Status.FLAGGED and done.flag_reason == "cap"
    assert money.ledger_balance(world.conn, world.b) == 4_500
    assert money.available_balance(world.conn, world.a, NOW) == 3_000


def test_ineligible_recipient_is_flagged(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    ghost = money.add_team(world.conn, world.program_id, "Ghost", eligible=False)

    done = move(world, "inel-1", world.a, ghost, 500)

    assert_refused_without_ledger_change(world, done, "ineligible", 3_000)
    assert money.ledger_balance(world.conn, ghost) == 0


def test_expired_program_refuses_apply(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)

    done = move(world, "exp-1", world.a, world.b, 500, now=NOW + 7_200)

    assert_refused_without_ledger_change(world, done, "expired", 3_000)


def test_cross_program_team_is_refused(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    other = money.create_program(world.conn, "Other", BUDGET, CAP, NOW + 3600, now=NOW)
    stranger = money.add_team(world.conn, other, "Stranger")

    done = move(world, "xp-1", world.a, stranger, 500)

    assert_refused_without_ledger_change(world, done, "program_mismatch", 3_000)
    assert money.ledger_balance(world.conn, stranger) == 0


def test_return_refused_when_program_disallows_returns(world):
    closed = money.create_program(
        world.conn, "Closed", BUDGET, CAP, NOW + 3600, returns_allowed=False, now=NOW
    )
    team = money.add_team(world.conn, closed, "Solo")
    money.grant(world.conn, team, 1_000, now=NOW)
    op = money.create_return(world.conn, closed, team, 500, "ret-closed", now=NOW)

    done = money.process_operation(world.conn, op.id, now=NOW)

    assert done.status is Status.FLAGGED and done.flag_reason == "returns_disabled"
    assert money.ledger_balance(world.conn, team) == 1_000


def test_v2_apply_without_reservation_is_refused(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    op = money.create_return(world.conn, world.program_id, world.a, 500, "noresv", now=NOW)

    result = money.apply_reallocation_v2(world.conn, op.id, now=NOW)

    assert not result.ok and result.rule == "no_reservation"
    assert money.get_operation(world.conn, op.id).status is Status.FLAGGED
    assert money.ledger_balance(world.conn, world.a) == 3_000


def test_concurrent_drain_applies_at_most_one(world):
    money.grant(world.conn, world.a, 1_000, now=NOW)
    workers = 8
    keys = [f"drain-{i}" for i in range(workers)]
    ops = [
        money.create_operation(
            world.conn, program_id=world.program_id, type="reallocate", from_team_id=world.a,
            to_team_id=world.b, amount_cents=1_000, idempotency_key=k, now=NOW,
        ).id
        for k in keys
    ]
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def run(op_id):
        conn = db.connect(world.path)
        try:
            barrier.wait()
            money.process_operation(conn, op_id, now=NOW)
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(op_id,)) for op_id in ops]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    statuses = [money.get_operation(world.conn, op_id).status for op_id in ops]
    assert statuses.count(Status.RECONCILED) == 1
    assert statuses.count(Status.FLAGGED) == workers - 1
    assert money.ledger_balance(world.conn, world.a) == 0
    assert money.ledger_balance(world.conn, world.b) == 1_000
    assert money.available_balance(world.conn, world.a, NOW) == 0


def test_second_reserve_on_a_drained_team_is_flagged_before_any_apply(world):
    money.grant(world.conn, world.a, 1_000, now=NOW)
    ops = [
        money.create_return(world.conn, world.program_id, world.a, 1_000, k, now=NOW)
        for k in ("r1", "r2")
    ]

    first = money.reserve_funds(world.conn, ops[0].id, now=NOW)
    second = money.reserve_funds(world.conn, ops[1].id, now=NOW)

    assert first.ok
    assert not second.ok and second.rule == "insufficient_available"
    assert money.get_operation(world.conn, ops[1].id).status is Status.FLAGGED
    assert money.available_balance(world.conn, world.a, NOW) == 0


def test_duplicate_idempotency_key_returns_original_operation(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    first = money.create_return(world.conn, world.program_id, world.a, 1_000, "dup", now=NOW)
    again = money.create_return(world.conn, world.program_id, world.a, 1_000, "dup", now=NOW)
    assert again.id == first.id

    money.process_operation(world.conn, first.id, now=NOW)
    money.process_operation(world.conn, again.id, now=NOW)

    assert money.ledger_balance(world.conn, world.a) == 2_000
    assert money.ledger_balance(world.conn, world.pool) == 1_000
    assert world.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1


def test_stale_reservation_releases_and_applies_nothing(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    op = money.create_return(world.conn, world.program_id, world.a, 1_000, "stale", now=NOW)
    assert money.reserve_funds(world.conn, op.id, ttl_seconds=10, now=NOW).ok
    assert money.available_balance(world.conn, world.a, NOW + 1) == 2_000

    assert money.available_balance(world.conn, world.a, NOW + 11) == 3_000
    assert money.release_expired_reservations(world.conn, now=NOW + 11) == 1

    after = money.get_operation(world.conn, op.id)
    assert after.status is Status.REQUESTED
    assert money.ledger_balance(world.conn, world.a) == 3_000
    assert money.ledger_balance(world.conn, world.pool) == 0

    retried = money.process_operation(world.conn, op.id, now=NOW + 12)
    assert retried.status is Status.RECONCILED
    assert money.ledger_balance(world.conn, world.pool) == 1_000


def test_apply_after_reservation_expired_is_refused(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    op = money.create_return(world.conn, world.program_id, world.a, 1_000, "late", now=NOW)
    money.reserve_funds(world.conn, op.id, ttl_seconds=10, now=NOW)

    result = money.apply_reallocation_v2(world.conn, op.id, now=NOW + 11)

    assert not result.ok and result.rule == "no_reservation"
    assert money.ledger_balance(world.conn, world.a) == 3_000
    assert money.ledger_balance(world.conn, world.pool) == 0


def test_v1_double_spends_where_v2_blocks(world):
    money.grant(world.conn, world.a, 1_000, now=NOW)
    first = move(world, "v1-a", world.a, world.b, 1_000, version="v1")
    second = move(world, "v1-b", world.a, world.b, 1_000, version="v1")

    assert first.status is Status.RECONCILED
    assert second.status is Status.FAILED
    assert money.ledger_balance(world.conn, world.a) == -1_000
    assert money.ledger_balance(world.conn, world.b) == 2_000
    assert money.available_balance(world.conn, world.a, NOW) < 0

    v2 = money.create_program(world.conn, "Fresh", BUDGET, CAP, NOW + 3600, now=NOW)
    x, y = money.add_team(world.conn, v2, "X"), money.add_team(world.conn, v2, "Y")
    money.grant(world.conn, x, 1_000, now=NOW)
    ops = [
        money.create_operation(
            world.conn, program_id=v2, type="reallocate", from_team_id=x, to_team_id=y,
            amount_cents=1_000, idempotency_key=k, now=NOW,
        )
        for k in ("v2-a", "v2-b")
    ]
    outcomes = [money.process_operation(world.conn, o.id, now=NOW).status for o in ops]
    assert outcomes == [Status.RECONCILED, Status.FLAGGED]
    assert money.ledger_balance(world.conn, x) == 0
    assert money.ledger_balance(world.conn, y) == 1_000


def test_v1_skips_eligibility_and_cap_checks(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    ghost = money.add_team(world.conn, world.program_id, "Ghost", eligible=False)

    to_ghost = move(world, "v1-ghost", world.a, ghost, 500, version="v1")
    assert to_ghost.status is Status.RECONCILED
    assert money.ledger_balance(world.conn, ghost) == 500

    money.grant(world.conn, world.b, 4_500, now=NOW)
    over_cap = move(world, "v1-cap", world.a, world.b, 1_000, version="v1")
    assert over_cap.status is Status.FAILED
    assert money.ledger_balance(world.conn, world.b) == 5_500
    assert "reconcile_failed" in money.audit_steps(world.conn, over_cap.id)


def test_v1_ignores_expiry_and_program_boundaries(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)

    late = move(world, "v1-late", world.a, world.b, 500, version="v1", now=NOW + 7_200)

    assert late.status is Status.RECONCILED
    assert money.ledger_balance(world.conn, world.b) == 500


def test_reconcile_fails_when_ledger_disagrees_with_operation(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    op = money.create_return(world.conn, world.program_id, world.a, 1_000, "tamper", now=NOW)
    money.reserve_funds(world.conn, op.id, now=NOW)
    money.apply_reallocation_v2(world.conn, op.id, now=NOW)
    world.conn.execute(
        "INSERT INTO issuer_ledger (program_id, team_id, amount_cents, kind, operation_id,"
        " created_at) VALUES (?, ?, 700, 'transfer_in', ?, ?)",
        (world.program_id, world.b, op.id, NOW),
    )

    result = money.reconcile(world.conn, op.id, now=NOW)

    assert not result.ok and "ledger_pair" in result.mismatches
    assert money.get_operation(world.conn, op.id).status is Status.FAILED


def test_audit_events_cannot_be_updated_or_deleted(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    op = money.create_return(world.conn, world.program_id, world.a, 1_000, "aud", now=NOW)
    money.write_audit_event(world.conn, op.id, "agent", "note", "hello", now=NOW)

    with pytest.raises(sqlite3.DatabaseError):
        world.conn.execute("UPDATE allocation_events SET step = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        world.conn.execute("DELETE FROM allocation_events")
    assert money.audit_steps(world.conn, op.id) == ["created", "note"]


def test_flag_for_admin_releases_reservation(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    op = money.create_return(world.conn, world.program_id, world.a, 1_000, "flag", now=NOW)
    money.reserve_funds(world.conn, op.id, now=NOW)

    assert money.flag_for_admin(world.conn, op.id, "suspicious", now=NOW).ok

    assert money.get_operation(world.conn, op.id).status is Status.FLAGGED
    assert money.available_balance(world.conn, world.a, NOW) == 3_000
