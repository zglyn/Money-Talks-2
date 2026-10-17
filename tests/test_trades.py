"""Offer, Trade, and off-ledger Settlement at the Operation-processing seam."""

import sqlite3

import pytest

from creditpool import db, money
from creditpool.money import OfferStatus, SettlementStatus, Status

from test_money import BUDGET, CAP, NOW, World, ledger_total


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


def ledger_rows(conn):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT team_id, amount_cents, kind, operation_id FROM issuer_ledger ORDER BY id"
        )
    ]


def counts(conn):
    offers = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    ops = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    return offers, trades, ops


def test_post_and_cancel_never_move_credit(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    before = ledger_rows(world.conn)

    offer = money.post_offer(
        world.conn, world.program_id, world.a, 1_000, 2_500, "off-1", note="outside", now=NOW,
    )
    again = money.post_offer(
        world.conn, world.program_id, world.a, 9_000, 1, "off-1", now=NOW,
    )

    assert again.id == offer.id
    assert again.amount_cents == 1_000
    assert again.status is OfferStatus.OPEN
    cancelled = money.cancel_offer(world.conn, offer.id, now=NOW)
    repeated = money.cancel_offer(world.conn, offer.id, now=NOW)

    assert cancelled.status is OfferStatus.CANCELLED
    assert repeated.id == offer.id
    assert ledger_rows(world.conn) == before
    assert counts(world.conn) == (1, 0, 0)
    with pytest.raises(ValueError):
        money.accept_offer(world.conn, offer.id, world.b, "acc-no", now=NOW)
    assert ledger_rows(world.conn) == before
    assert money.audit_steps_for_offer(world.conn, offer.id) == ["offer_created", "offer_cancelled"]


def test_accept_reallocates_once_and_settlement_does_not_move_credit(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    offer = money.post_offer(
        world.conn, world.program_id, world.a, 1_000, 2_500, "off-1", note="outside", now=NOW,
    )
    before_accept = ledger_rows(world.conn)

    trade = money.accept_offer(world.conn, offer.id, world.b, "acc-1", now=NOW)
    again = money.accept_offer(world.conn, offer.id, world.b, "acc-1", now=NOW)

    assert again.id == trade.id
    assert again.operation_id == trade.operation_id
    assert money.get_offer(world.conn, offer.id).status is OfferStatus.ACCEPTED
    assert ledger_rows(world.conn) == before_accept
    assert counts(world.conn) == (1, 1, 1)
    op = money.get_operation(world.conn, trade.operation_id)
    assert op.type == "reallocate"
    assert (op.from_team_id, op.to_team_id, op.amount_cents) == (world.a, world.b, 1_000)
    assert op.status is Status.REQUESTED
    assert trade.settlement_status is SettlementStatus.SIMULATED
    assert trade.settlement_ask_cents == 2_500

    done = money.process_operation(world.conn, trade.operation_id, now=NOW)
    money.process_operation(world.conn, trade.operation_id, now=NOW)

    assert done.status is Status.RECONCILED
    assert money.ledger_balance(world.conn, world.a) == 2_000
    assert money.ledger_balance(world.conn, world.b) == 1_000
    assert money.program_total(world.conn, world.program_id) == 3_000
    assert ledger_total(world.conn) == 3_000
    after_credit = ledger_rows(world.conn)

    paid = money.record_settlement(world.conn, trade.id, "external_paid", now=NOW)
    repeat = money.record_settlement(world.conn, trade.id, "external_paid", now=NOW)

    assert paid.settlement_status is SettlementStatus.EXTERNAL_PAID
    assert repeat.settlement_status is SettlementStatus.EXTERNAL_PAID
    assert ledger_rows(world.conn) == after_credit
    assert money.audit_steps_for_offer(world.conn, offer.id) == [
        "offer_created", "offer_accepted", "settlement",
    ]
    steps = money.audit_steps(world.conn, trade.operation_id)
    assert steps.count("applied") == 1
    assert "offer_accepted" in steps
    assert steps.count("settlement") == 1


def test_repeated_accept_key_returns_the_first_trade(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    first = money.post_offer(world.conn, world.program_id, world.a, 100, 50, "off-1", now=NOW)
    second = money.post_offer(world.conn, world.program_id, world.a, 200, 50, "off-2", now=NOW)

    trade = money.accept_offer(world.conn, first.id, world.b, "same-key", now=NOW)
    collapsed = money.accept_offer(world.conn, second.id, world.b, "same-key", now=NOW)

    assert collapsed.id == trade.id
    assert money.get_offer(world.conn, second.id).status is OfferStatus.OPEN
    assert counts(world.conn) == (2, 1, 1)


def test_second_key_cannot_accept_an_accepted_offer(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    offer = money.post_offer(world.conn, world.program_id, world.a, 100, 50, "off-1", now=NOW)
    money.accept_offer(world.conn, offer.id, world.b, "k1", now=NOW)

    with pytest.raises(ValueError):
        money.accept_offer(world.conn, offer.id, world.b, "k2", now=NOW)

    assert counts(world.conn) == (1, 1, 1)


def test_accept_refuses_a_key_already_used_by_an_operation(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    before = ledger_rows(world.conn)
    money.create_return(world.conn, world.program_id, world.a, 100, "taken", now=NOW)
    offer = money.post_offer(world.conn, world.program_id, world.a, 100, 50, "off-1", now=NOW)

    with pytest.raises(ValueError):
        money.accept_offer(world.conn, offer.id, world.b, "taken", now=NOW)

    assert money.get_offer(world.conn, offer.id).status is OfferStatus.OPEN
    assert counts(world.conn) == (1, 0, 1)
    assert ledger_rows(world.conn) == before


def test_accept_by_the_posting_team_is_refused(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    before = ledger_rows(world.conn)
    offer = money.post_offer(world.conn, world.program_id, world.a, 100, 50, "off-1", now=NOW)

    with pytest.raises(ValueError):
        money.accept_offer(world.conn, offer.id, world.a, "k1", now=NOW)

    assert money.get_offer(world.conn, offer.id).status is OfferStatus.OPEN
    assert ledger_rows(world.conn) == before
    assert counts(world.conn) == (1, 0, 0)


def _accept(world, src, dst, amount, key):
    offer = money.post_offer(
        world.conn, world.program_id, src, amount, 100, f"off-{key}", now=NOW,
    )
    return money.accept_offer(world.conn, offer.id, dst, f"acc-{key}", now=NOW)


def test_insufficient_available_is_flagged_and_settlement_still_moves_nothing(world):
    money.grant(world.conn, world.a, 400, now=NOW)
    before = ledger_rows(world.conn)
    trade = _accept(world, world.a, world.b, 1_000, "short")

    done = money.process_operation(world.conn, trade.operation_id, now=NOW)
    paid = money.record_settlement(world.conn, trade.id, "external_paid", now=NOW)

    assert done.status is Status.FLAGGED
    assert done.flag_reason == "insufficient_available"
    assert paid.settlement_status is SettlementStatus.EXTERNAL_PAID
    assert ledger_rows(world.conn) == before
    assert "applied" not in money.audit_steps(world.conn, done.id)
    assert money.audit_steps(world.conn, done.id)[-1] == "settlement"


def test_over_cap_recipient_is_flagged(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    money.grant(world.conn, world.b, 4_500, now=NOW)
    before = ledger_rows(world.conn)
    trade = _accept(world, world.a, world.b, 1_000, "cap")

    done = money.process_operation(world.conn, trade.operation_id, now=NOW)

    assert done.status is Status.FLAGGED
    assert done.flag_reason == "cap"
    assert ledger_rows(world.conn) == before
    assert money.available_balance(world.conn, world.a, NOW) == 3_000
    assert "applied" not in money.audit_steps(world.conn, done.id)


def test_expired_program_is_flagged(tmp_path):
    conn = db.connect(str(tmp_path / "old.db"))
    db.init_schema(conn)
    program_id = money.create_program(conn, "Old", BUDGET, CAP, NOW - 1, now=NOW)
    a = money.add_team(conn, program_id, "Alpha")
    b = money.add_team(conn, program_id, "Beta")
    money.grant(conn, a, 3_000, now=NOW)
    before = ledger_rows(conn)
    offer = money.post_offer(conn, program_id, a, 500, 100, "off-1", now=NOW)
    trade = money.accept_offer(conn, offer.id, b, "acc-1", now=NOW)

    done = money.process_operation(conn, trade.operation_id, now=NOW)

    assert done.status is Status.FLAGGED
    assert done.flag_reason == "expired"
    assert ledger_rows(conn) == before
    conn.close()


def test_ineligible_recipient_is_flagged(world):
    closed = money.add_team(world.conn, world.program_id, "Closed", eligible=False)
    money.grant(world.conn, world.a, 3_000, now=NOW)
    before = ledger_rows(world.conn)
    trade = _accept(world, world.a, closed, 500, "closed")

    done = money.process_operation(world.conn, trade.operation_id, now=NOW)

    assert done.status is Status.FLAGGED
    assert done.flag_reason == "ineligible"
    assert ledger_rows(world.conn) == before


def test_cross_program_accept_is_flagged(world):
    other = money.create_program(world.conn, "Other", BUDGET, CAP, NOW + 3600, now=NOW)
    gamma = money.add_team(world.conn, other, "Gamma")
    money.grant(world.conn, world.a, 3_000, now=NOW)
    before = ledger_rows(world.conn)
    trade = _accept(world, world.a, gamma, 500, "cross")

    done = money.process_operation(world.conn, trade.operation_id, now=NOW)

    assert done.status is Status.FLAGGED
    assert done.flag_reason == "program_mismatch"
    assert ledger_rows(world.conn) == before
    assert money.ledger_balance(world.conn, gamma) == 0


def test_cancel_of_an_accepted_offer_is_refused(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    offer = money.post_offer(world.conn, world.program_id, world.a, 100, 50, "off-1", now=NOW)
    money.accept_offer(world.conn, offer.id, world.b, "k1", now=NOW)

    with pytest.raises(ValueError):
        money.cancel_offer(world.conn, offer.id, now=NOW)

    assert money.get_offer(world.conn, offer.id).status is OfferStatus.ACCEPTED


def test_audit_upgrade_keeps_events_and_allows_an_offer_step(tmp_path):
    conn = db.connect(str(tmp_path / "old.db"))
    db.init_schema(conn)
    program_id = money.create_program(conn, "Hackathon", BUDGET, CAP, NOW + 3600, now=NOW)
    team = money.add_team(conn, program_id, "Alpha")
    money.grant(conn, team, 1_000, now=NOW)
    op = money.create_return(conn, program_id, team, 100, "ret-old", now=NOW)
    conn.executescript(
        """
        DROP TABLE allocation_events;
        CREATE TABLE allocation_events (
            id INTEGER PRIMARY KEY,
            operation_id INTEGER NOT NULL REFERENCES operations (id),
            actor TEXT NOT NULL,
            step TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO allocation_events (operation_id, actor, step, detail, created_at)"
        " VALUES (?, 'system', 'created', 'return', ?)",
        (op.id, NOW),
    )

    db.init_schema(conn)

    kept = conn.execute(
        "SELECT step, offer_id FROM allocation_events ORDER BY id"
    ).fetchone()
    assert kept["step"] == "created"
    assert kept["offer_id"] is None
    conn.execute(
        "INSERT INTO allocation_events (operation_id, offer_id, actor, step, detail, created_at)"
        " VALUES (NULL, NULL, 'team', 'offer_created', '', ?)",
        (NOW,),
    )
    assert conn.execute("SELECT COUNT(*) FROM allocation_events").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE allocation_events SET detail = 'changed'")
    conn.close()


def test_bad_settlement_status_is_refused(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    offer = money.post_offer(world.conn, world.program_id, world.a, 100, 50, "off-1", now=NOW)
    with pytest.raises(ValueError):
        money.accept_offer(
            world.conn, offer.id, world.b, "k1", settlement_status="custodied", now=NOW,
        )
    assert counts(world.conn) == (1, 0, 0)
