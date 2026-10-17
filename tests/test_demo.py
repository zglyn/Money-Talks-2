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
    for path in ("/", "/wallet", "/operations", "/reconciliation", "/audit", "/demo"):
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
    page = client.get("/wallet").text
    return int(page.split('<option value="')[1].split('"')[0])
