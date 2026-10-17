"""Program pulse numbers from Available Balance, on the Program console."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from creditpool import db, money, report, seed
from creditpool.app import create_app

NOW = 1_800_000_000.0


def connect(tmp_path: Path):
    conn = db.connect(str(tmp_path / "pulse.db"))
    db.init_schema(conn)
    return conn


def pulse_section(html: str, program_id: int) -> str:
    marker = f'data-program-id="{program_id}"'
    start = html.rindex("<section", 0, html.index(marker))
    end = html.index("</section>", start) + len("</section>")
    return html[start:end]


def attr(section: str, name: str) -> str:
    match = re.search(rf'{name}="([^"]*)"', section)
    assert match, section
    return match.group(1)


def team_ids(section: str, name: str) -> tuple[int, ...]:
    raw = attr(section, name)
    if raw == "":
        return ()
    return tuple(int(part) for part in raw.split(","))


def href(section: str, anchor: str) -> str:
    match = re.search(rf'href="([^"]*#{anchor})"', section)
    assert match, section
    return match.group(1)


def build_pressure(conn):
    program_id = money.create_program(
        conn, "Pulse Program", 100_000, 50_000, NOW + 7_200, now=NOW,
    )
    rich = money.add_team(conn, program_id, "Rich")
    partial = money.add_team(conn, program_id, "Partial")
    blocked = money.add_team(conn, program_id, "Blocked", eligible=False)
    held = money.add_team(conn, program_id, "Held")
    empty = money.add_team(conn, program_id, "Empty")
    pool = money.pool_team_id(conn, program_id)
    money.grant(conn, rich, 20_000, now=NOW)
    money.grant(conn, partial, 10_000, now=NOW)
    money.grant(conn, blocked, 5_000, now=NOW)
    money.grant(conn, held, 8_000, now=NOW)
    money.grant(conn, pool, 1_000, now=NOW)
    held_op = money.create_return(conn, program_id, held, 8_000, "hold-all", now=NOW)
    partial_op = money.create_return(conn, program_id, partial, 1_000, "hold-some", now=NOW)
    assert money.reserve_funds(conn, held_op.id, ttl_seconds=300, now=NOW).ok
    assert money.reserve_funds(conn, partial_op.id, ttl_seconds=10_000, now=NOW).ok
    return {
        "program_id": program_id,
        "rich": rich,
        "partial": partial,
        "blocked": blocked,
        "held": held,
        "empty": empty,
        "pool": pool,
    }


def test_pulse_uses_available_balance_and_active_reservations(tmp_path):
    conn = connect(tmp_path)
    ids = build_pressure(conn)
    dry_only = money.create_program(conn, "Dry Only", 10_000, 5_000, NOW + 60, now=NOW)
    lone = money.add_team(conn, dry_only, "Lone")

    view = next(v for v in report.programs(conn, now=NOW) if v.id == ids["program_id"])
    held = next(t for t in view.teams if t.id == ids["held"])
    partial = next(t for t in view.teams if t.id == ids["partial"])
    blocked = next(t for t in view.teams if t.id == ids["blocked"])
    pool = next(t for t in view.teams if t.id == ids["pool"])

    assert held.ledger_cents == 8_000
    assert held.available_cents == 0
    assert partial.ledger_cents == 10_000
    assert partial.available_cents == 9_000
    assert blocked.eligible is False
    assert pool.ledger_cents == 1_000
    assert view.pulse.seconds_to_expiry == 7_200
    assert view.pulse.expiry_label == "2h"
    assert view.pulse.surplus_teams == (ids["rich"], ids["partial"], ids["blocked"])
    assert view.pulse.dry_teams == (ids["held"], ids["empty"])
    assert ids["pool"] not in view.pulse.surplus_teams
    assert ids["pool"] not in view.pulse.dry_teams
    assert view.pulse.return_href == f"/wallet?team_id={ids['rich']}#return"
    assert view.pulse.request_href == f"/wallet?team_id={ids['held']}#request"
    assert view.pulse.offer_href == "/trades"

    later = next(v for v in report.programs(conn, now=NOW + 300) if v.id == ids["program_id"])
    assert later.pulse.seconds_to_expiry == 6_900
    assert later.pulse.surplus_teams == (
        ids["rich"], ids["partial"], ids["blocked"], ids["held"],
    )
    assert later.pulse.dry_teams == (ids["empty"],)
    assert money.ledger_balance(conn, ids["held"]) == 8_000
    assert next(t for t in later.teams if t.id == ids["partial"]).available_cents == 9_000

    only = next(v for v in report.programs(conn, now=NOW) if v.id == dry_only)
    assert only.pulse.surplus_teams == ()
    assert only.pulse.dry_teams == (lone,)
    assert only.pulse.return_href == "/wallet#return"
    assert only.pulse.request_href == f"/wallet?team_id={lone}#request"
    assert only.pulse.seconds_to_expiry == 60
    conn.close()


def test_seeded_demo_pulse_counts_every_granted_team(tmp_path):
    conn = connect(tmp_path)
    seed.seed_demo(conn, now=NOW)
    views = report.programs(conn, now=NOW)
    live = next(v for v in views if v.name == seed.PROGRAM_NAME)
    expired = next(v for v in views if v.name == seed.EXPIRED_PROGRAM_NAME)

    assert live.pulse.seconds_to_expiry == 7 * 86_400
    assert live.pulse.expiry_label == "7d"
    assert live.pulse.surplus_teams == tuple(sorted(seed.demo_world(conn).teams.values()))
    assert live.pulse.dry_teams == ()
    assert live.pulse.surplus_count == 12
    assert live.pulse.dry_count == 0
    assert expired.pulse.seconds_to_expiry == 0
    assert expired.pulse.expiry_label == "Expired"
    assert expired.pulse.surplus_count == 3
    assert expired.pulse.dry_count == 0
    conn.close()


def _client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("creditpool.report.time.time", lambda: NOW)
    return TestClient(create_app(str(tmp_path / "pulse.db")))


def test_console_renders_pulse_numbers_and_action_links(tmp_path, monkeypatch):
    conn = connect(tmp_path)
    ids = build_pressure(conn)
    conn.close()
    client = _client(tmp_path, monkeypatch)
    section = pulse_section(client.get("/").text, ids["program_id"])

    assert "Program pulse" in section
    assert team_ids(section, "data-surplus-team-ids") == (
        ids["rich"], ids["partial"], ids["blocked"],
    )
    assert team_ids(section, "data-dry-team-ids") == (ids["held"], ids["empty"])
    assert attr(section, "data-seconds-to-expiry") == "7200"
    assert "7200 seconds" in section
    assert re.search(r"Surplus Teams</div>\s*<div class=\"value\">3</div>", section)
    assert re.search(r"Dry Teams</div>\s*<div class=\"value\">2</div>", section)
    assert "Available Balance" in section
    assert "Reservations" in section
    assert 'href="/trades"' in section
    assert ">Offer</a>" in section

    return_href = href(section, "return")
    request_href = href(section, "request")
    assert return_href == f"/wallet?team_id={ids['rich']}#return"
    assert request_href == f"/wallet?team_id={ids['held']}#request"

    return_page = client.get(return_href.split("#", 1)[0])
    assert return_page.status_code == 200
    assert 'id="return"' in return_page.text
    assert f'name="team_id" value="{ids["rich"]}"' in return_page.text
    returned = client.post(
        "/wallet/return",
        data={"team_id": ids["rich"], "amount": "10.00", "key": "pulse-return"},
        follow_redirects=True,
    )
    assert returned.status_code == 200
    assert "Return queued" in returned.text

    request_page = client.get(request_href.split("#", 1)[0])
    assert request_page.status_code == 200
    assert 'id="request"' in request_page.text
    assert f'name="team_id" value="{ids["held"]}"' in request_page.text
    requested = client.post(
        "/wallet/request",
        data={"team_id": ids["held"], "amount": "10.00", "key": "pulse-request"},
        follow_redirects=True,
    )
    assert requested.status_code == 200
    assert "Request queued" in requested.text


def test_seeded_console_shows_pulse_for_both_programs(tmp_path, monkeypatch):
    conn = connect(tmp_path)
    seed.seed_demo(conn, now=NOW)
    world = seed.demo_world(conn)
    conn.close()
    client = _client(tmp_path, monkeypatch)
    html = client.get("/").text
    live = pulse_section(html, world.program_id)
    expired = pulse_section(html, world.expired_program_id)

    assert attr(live, "data-seconds-to-expiry") == "604800"
    assert "604800 seconds" in live
    assert team_ids(live, "data-surplus-team-ids") == tuple(sorted(world.teams.values()))
    assert team_ids(live, "data-dry-team-ids") == ()
    assert re.search(r"Surplus Teams</div>\s*<div class=\"value\">12</div>", live)
    assert re.search(r"Dry Teams</div>\s*<div class=\"value\">0</div>", live)
    assert href(live, "return") == f"/wallet?team_id={min(world.teams.values())}#return"
    assert href(live, "request") == "/wallet#request"
    assert 'href="/trades"' in live

    assert attr(expired, "data-seconds-to-expiry") == "0"
    assert ">Expired</div>" in expired
    assert len(team_ids(expired, "data-surplus-team-ids")) == 3
    assert team_ids(expired, "data-dry-team-ids") == ()

    wallet = client.get("/wallet#request")
    assert wallet.status_code == 200
    assert 'id="request"' in wallet.text
    assert 'id="return"' in wallet.text
