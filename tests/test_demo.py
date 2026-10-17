"""Seed scenarios and the FastAPI demo, end to end against a temporary SQLite file."""

import pytest
from fastapi.testclient import TestClient

from creditpool import db, money, report, seed
from creditpool.app import create_app
from creditpool.worker import claim_and_process_loop


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "demo.db"))
    db.init_schema(c)
    seed.seed_demo(c)
    yield c
    c.close()


def dirty_teams(conn):
    return [t.name for p in report.programs(conn) for t in p.teams if t.mismatches]


def test_seed_matches_the_money_talks_2_shape(conn):
    world = seed.demo_world(conn)

    assert len(world.teams) == 12
    assert all(money.ledger_balance(conn, tid) == 26_000 for tid in world.teams.values())
    assert money.ledger_balance(conn, world.pool_id) == 0
    assert seed.seed_demo(conn) == world
    assert conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0] == 2


def test_batch_is_about_sixty_operations_with_collapsed_duplicates(conn):
    batch = seed.insert_batch(conn, "v2")

    assert 55 <= len(batch.operation_ids) <= 65
    assert batch.duplicates_collapsed == 6
    assert seed.insert_batch(conn, "v2").operation_ids == batch.operation_ids


def test_v2_batch_has_no_failures_and_a_clean_reconciliation(conn):
    seed.insert_batch(conn, "v2")

    runs = claim_and_process_loop(conn, "w")

    assert all(r.ok for r in runs)
    assert all(len(r.steps) >= 6 for r in runs)
    assert {r.error_code for r in runs if r.outcome == "flagged"} == {
        "cap", "ineligible", "expired", "insufficient_balance",
    }
    assert dirty_teams(conn) == []
    assert report.failed_reconciles(conn) == []


def test_v1_batch_shows_money_failures_and_reset_allows_a_v2_rerun(conn):
    snap = seed.snapshot(conn)
    seed.insert_batch(conn, "v1")

    v1_runs = claim_and_process_loop(conn, "w")

    assert any(r.outcome == "failed" for r in v1_runs)
    assert dirty_teams(conn)

    seed.restore(conn, snap)
    assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    assert dirty_teams(conn) == []

    seed.insert_batch(conn, "v2")
    assert all(r.ok for r in claim_and_process_loop(conn, "w"))
    assert dirty_teams(conn) == []


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "ui.db")))


def test_every_page_renders_brand(client):
    for path in ("/", "/wallet", "/trades", "/operations", "/reconciliation", "/audit", "/demo"):
        response = client.get(path)
        assert response.status_code == 200
        assert "CreditPool" in response.text


def test_demo_safe_batch_keeps_reconciliation_clean(client):
    assert client.post("/demo/seed").status_code == 200
    client.post("/demo/run", follow_redirects=True)
    page = client.get("/reconciliation").text
    assert "Clean" in page
    assert "created" in client.get("/audit?operation_id=1").text


def test_one_click_full_demo_seeds_and_runs_v2(client):
    page = client.post("/demo/run-full", follow_redirects=True).text
    assert "Demo ready" in page or "Batch done" in page or "flagged" in page
    assert "Clean" in client.get("/reconciliation").text


def test_admin_reallocate_and_audit_csv(client):
    client.post("/demo/seed")
    html = client.get("/").text
    # first non-pool team options exist on console
    assert "Admin Reallocate" in html
    teams = client.get("/wallet").text
    a = int(teams.split('<option value="')[1].split('"')[0])
    b = int(teams.split('<option value="')[2].split('"')[0])
    r = client.post(
        "/admin/reallocate",
        data={"program_id": 1, "from_team_id": a, "to_team_id": b, "amount": "5.00", "key": "adm1"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    client.post("/operations/process")
    csv = client.get("/audit.csv").text
    assert "operation_id" in csv and "created" in csv


def test_wallet_return_is_idempotent_and_processed_from_the_queue(client):
    client.post("/demo/seed")
    team_id = seed_team_id(client)
    form = {"team_id": team_id, "amount": "10.00", "key": "same"}

    client.post("/wallet/return", data=form)
    client.post("/wallet/return", data=form)
    client.post("/operations/process")

    page = client.get("/operations").text
    assert page.count("reconciled") >= 1
    assert "$10.00" in page
    assert page.count("<td>return</td>") == 1


def seed_team_id(client) -> int:
    return team_ids(client)[0]


def team_ids(client) -> list[int]:
    page = client.get("/wallet").text
    return [int(part.split('"')[0]) for part in page.split('<option value="')[1:]]


def ledger_stat(client, team_id: int) -> str:
    page = client.get(f"/wallet?team_id={team_id}").text
    chunk = page.split("Ledger balance", 1)[1][:180]
    start = chunk.index("$")
    end = chunk.index("</div>", start)
    return chunk[start:end]


def test_trade_board_queues_or_approves_a_reallocate_without_moving_settlement(client):
    client.post("/demo/seed")
    poster, receiver = team_ids(client)[:2]
    assert ledger_stat(client, poster) == "$260.00"
    assert ledger_stat(client, receiver) == "$260.00"

    board = client.get("/trades").text
    assert "off-ledger" in board and "not custodied" in board
    assert "buy" not in board.lower() and "sell" not in board.lower()

    form = {
        "team_id": poster, "amount": "10.00", "settlement_ask": "4.00",
        "note": "pay outside", "key": "offer-1",
    }
    client.post("/trades/offers", data=form)
    posted = client.post("/trades/offers", data=form, follow_redirects=True).text
    assert posted.count('name="offer_id"') == 1
    assert "pay outside" in posted and "$10.00" in posted and "$4.00" in posted
    offer_id = posted.split('name="offer_id" value="')[1].split('"')[0]

    queued = client.post(
        "/trades/accept",
        data={"offer_id": offer_id, "to_team_id": receiver, "key": "acc-queue"},
        follow_redirects=True,
    ).text
    assert "queued Reallocate" in queued
    assert ledger_stat(client, poster) == "$260.00"

    ops = client.get("/operations").text
    assert 'class="tag requested"' in ops
    trade_id = queued.split("Trade #")[1].split(" ")[0]
    assert f'href="/trades">{trade_id}</a>' in ops

    client.post("/operations/process")
    assert ledger_stat(client, poster) == "$250.00"
    assert ledger_stat(client, receiver) == "$270.00"
    done = client.get("/operations").text
    assert "reconciled" in done

    settled = client.post(
        f"/trades/{trade_id}/settlement",
        data={"status": "external_paid"},
        follow_redirects=True,
    ).text
    assert "Settlement external_paid" in settled
    assert "pay outside" in settled
    assert ledger_stat(client, poster) == "$250.00"
    assert ledger_stat(client, receiver) == "$270.00"

    audit = client.get("/audit").text
    assert "offer_created" in audit
    assert "offer_accepted" in audit
    assert "settlement" in audit

    again = client.post(
        "/trades/accept",
        data={"offer_id": offer_id, "to_team_id": receiver, "key": "acc-queue"},
        follow_redirects=True,
    )
    assert again.status_code == 200
    assert client.get("/operations").text.count("<td>reallocate</td>") == 1


def test_cancel_offer_stays_on_the_audit_log_and_moves_no_credit(client):
    client.post("/demo/seed")
    poster = team_ids(client)[0]
    client.post(
        "/trades/offers",
        data={
            "team_id": poster, "amount": "5.00", "settlement_ask": "1.00",
            "note": "", "key": "offer-cancel",
        },
    )
    page = client.get("/trades").text
    offer_id = page.split("/trades/offers/")[1].split("/cancel")[0]
    cancelled = client.post(f"/trades/offers/{offer_id}/cancel", follow_redirects=True).text
    assert "cancelled" in cancelled
    assert 'name="offer_id"' not in cancelled
    assert ledger_stat(client, poster) == "$260.00"
    assert "offer_cancelled" in client.get("/audit").text


def test_one_click_approve_uses_the_operations_processor(client):
    client.post("/demo/seed")
    poster, receiver = team_ids(client)[:2]
    client.post(
        "/trades/offers",
        data={
            "team_id": poster, "amount": "10.00", "settlement_ask": "0.00",
            "note": "recorded only", "key": "offer-approve",
        },
    )
    offer_id = client.get("/trades").text.split('name="offer_id" value="')[1].split('"')[0]
    approved = client.post(
        "/trades/accept",
        data={"offer_id": offer_id, "to_team_id": receiver, "key": "acc-approve", "approve": "1"},
        follow_redirects=True,
    ).text
    assert "reconciled" in approved
    assert ledger_stat(client, poster) == "$250.00"
    assert ledger_stat(client, receiver) == "$270.00"
