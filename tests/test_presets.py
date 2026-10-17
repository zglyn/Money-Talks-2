"""Policy Preset configure outcomes against the simulated Issuer Ledger."""

import pytest
from fastapi.testclient import TestClient

from creditpool import db, money, presets
from creditpool.app import create_app

NOW = 1_700_000_000.0

# The template contract. These numbers are the Program Rules a preset must fill.
TEMPLATES = {
    "Hackathon weekend": (1_000_000, 200_000, "listed", True, 48 * 3600),
    "Workshop day": (200_000, 50_000, "open", True, 8 * 3600),
    "Returns closed": (500_000, 100_000, "listed", False, 7 * 24 * 3600),
}


def fresh(tmp_path):
    conn = db.connect(str(tmp_path / "presets.db"))
    db.init_schema(conn)
    return conn


def rules_tuple(conn, program_id):
    row = conn.execute(
        "SELECT total_budget_cents, per_team_cap_cents, eligibility_stance, returns_allowed,"
        " expires_at, policy_preset FROM programs WHERE id = ?",
        (program_id,),
    ).fetchone()
    return (
        row["total_budget_cents"],
        row["per_team_cap_cents"],
        row["eligibility_stance"],
        bool(row["returns_allowed"]),
        row["expires_at"],
        row["policy_preset"],
    )


def ledger_rows(conn):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT id, program_id, team_id, amount_cents, kind FROM issuer_ledger ORDER BY id"
        )
    ]


def test_each_policy_preset_fills_program_rules(tmp_path):
    conn = fresh(tmp_path)
    for name, (budget, cap, stance, returns, offset) in TEMPLATES.items():
        program_id = money.create_program_from_preset(conn, name, name, now=NOW)
        budget_cents, cap_cents, got_stance, got_returns, expires_at, preset = rules_tuple(
            conn, program_id
        )
        assert (budget_cents, cap_cents, got_stance, got_returns, expires_at, preset) == (
            budget, cap, stance, returns, NOW + offset, name,
        )
        event = conn.execute(
            "SELECT actor, step, detail FROM program_audit_events WHERE program_id = ?",
            (program_id,),
        ).fetchone()
        assert event["actor"] == "admin"
        assert event["step"] == "Policy Preset applied"
        assert name in event["detail"]
    conn.close()


def test_hackathon_weekend_grants_sum_to_the_template_budget(tmp_path):
    conn = fresh(tmp_path)
    budget, cap = 1_000_000, 200_000
    program_id = money.create_program_from_preset(conn, "Weekend", "Hackathon weekend", now=NOW)

    for i in range(budget // cap):
        team_id = money.add_team(conn, program_id, f"Team {i}")
        money.grant(conn, team_id, cap, now=NOW)

    assert money.program_total(conn, program_id) == budget
    extra = money.add_team(conn, program_id, "Extra")
    with pytest.raises(money.RuleViolation) as refused:
        money.grant(conn, extra, 1, now=NOW)
    assert refused.value.rule == "budget"
    assert money.program_total(conn, program_id) == budget
    conn.close()


def test_admin_can_edit_filled_rules_before_grants(tmp_path):
    conn = fresh(tmp_path)
    program_id = money.create_program_from_preset(conn, "Weekend", "Hackathon weekend", now=NOW)

    money.update_program_rules(
        conn,
        program_id,
        presets.ProgramRules(
            total_budget_cents=1_000_000,
            per_team_cap_cents=75_000,
            eligibility_stance=presets.EligibilityStance.OPEN,
            returns_allowed=False,
            expires_at=NOW + 24 * 3600,
        ),
    )

    assert rules_tuple(conn, program_id) == (
        1_000_000, 75_000, "open", False, NOW + 24 * 3600, None,
    )
    events = conn.execute("SELECT step, detail FROM program_audit_events").fetchall()
    assert [e["step"] for e in events] == ["Policy Preset applied"]
    assert "Hackathon weekend" in events[0]["detail"]

    team_id = money.add_team(conn, program_id, "Alpha")
    money.grant(conn, team_id, 75_000, now=NOW)
    with pytest.raises(money.RuleViolation) as refused:
        money.grant(conn, team_id, 1, now=NOW)
    assert refused.value.rule == "cap"
    conn.close()


def test_same_preset_applied_twice_keeps_one_audit_event(tmp_path):
    conn = fresh(tmp_path)
    program_id = money.create_program_from_preset(conn, "Weekend", "Hackathon weekend", now=NOW)
    before = rules_tuple(conn, program_id)

    money.apply_policy_preset(conn, program_id, "Hackathon weekend", now=NOW)

    assert rules_tuple(conn, program_id) == before
    assert conn.execute("SELECT COUNT(*) FROM program_audit_events").fetchone()[0] == 1
    conn.close()


def test_preset_change_after_grants_keeps_grant_history(tmp_path):
    conn = fresh(tmp_path)
    program_id = money.create_program_from_preset(conn, "Weekend", "Hackathon weekend", now=NOW)
    team_id = money.add_team(conn, program_id, "Alpha")
    money.grant(conn, team_id, 50_000, now=NOW)
    before_rules = rules_tuple(conn, program_id)
    before_ledger = ledger_rows(conn)
    before_audit = [
        tuple(r)
        for r in conn.execute(
            "SELECT id, program_id, actor, step, detail FROM program_audit_events ORDER BY id"
        )
    ]

    with pytest.raises(money.RuleViolation) as preset_refused:
        money.apply_policy_preset(conn, program_id, "Returns closed", now=NOW + 10)
    assert preset_refused.value.rule == "grants_exist"

    with pytest.raises(money.RuleViolation) as edit_refused:
        money.update_program_rules(
            conn,
            program_id,
            presets.ProgramRules(
                total_budget_cents=500_000,
                per_team_cap_cents=100_000,
                eligibility_stance=presets.EligibilityStance.LISTED,
                returns_allowed=False,
                expires_at=NOW + 3600,
            ),
        )
    assert edit_refused.value.rule == "grants_exist"

    assert rules_tuple(conn, program_id) == before_rules
    assert ledger_rows(conn) == before_ledger
    assert [
        tuple(r)
        for r in conn.execute(
            "SELECT id, program_id, actor, step, detail FROM program_audit_events ORDER BY id"
        )
    ] == before_audit
    assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    conn.close()


def test_unknown_policy_preset_creates_nothing(tmp_path):
    conn = fresh(tmp_path)
    with pytest.raises(ValueError):
        money.create_program_from_preset(conn, "Nope", "Not a preset", now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0] == 0
    conn.close()


def test_existing_database_gains_preset_columns(tmp_path):
    conn = db.connect(str(tmp_path / "old.db"))
    conn.execute(
        """CREATE TABLE programs (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            total_budget_cents INTEGER NOT NULL,
            per_team_cap_cents INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            returns_allowed INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO programs (name, total_budget_cents, per_team_cap_cents, expires_at,"
        " returns_allowed, created_at) VALUES ('Old', 100, 50, 10, 1, 1)"
    )
    db.init_schema(conn)
    row = conn.execute("SELECT eligibility_stance, policy_preset FROM programs").fetchone()
    assert row["eligibility_stance"] == "open"
    assert row["policy_preset"] is None
    conn.close()


def test_configure_page_edits_before_grant_and_refuses_preset_after(tmp_path):
    client = TestClient(create_app(str(tmp_path / "ui.db")))

    first = client.get("/configure")
    assert first.status_code == 200
    assert "Policy Preset" in first.text
    assert "Program Rules" in first.text
    assert "Hackathon weekend" in first.text
    assert "Eligible Teams are an explicit list" in first.text

    applied = client.post(
        "/configure/preset",
        data={"name": "Weekend", "preset": "Hackathon weekend"},
        follow_redirects=True,
    )
    assert applied.status_code == 200
    assert "$10,000.00" in applied.text
    assert "$2,000.00" in applied.text
    assert "Policy Preset Hackathon weekend applied" in applied.text

    edited = client.post(
        "/configure/rules",
        data={
            "program_id": "1",
            "name": "Weekend",
            "budget": "10000.00",
            "cap": "750.00",
            "eligibility_stance": "open",
            "returns_allowed": "yes",
            "expiry_offset_hours": "24",
        },
        follow_redirects=True,
    )
    assert "set directly" in edited.text
    assert 'value="750.00"' in edited.text

    added = client.post(
        "/configure/team",
        data={"program_id": "1", "name": "Alpha", "eligible": "yes"},
        follow_redirects=True,
    )
    team_id = added.text.split('id="grant-team"', 1)[1].split('value="', 1)[1].split('"', 1)[0]
    granted = client.post(
        "/configure/grant",
        data={"program_id": "1", "team_id": team_id, "amount": "750.00"},
        follow_redirects=True,
    )
    assert "Grant recorded" in granted.text

    console = client.get("/").text
    assert "$10,000.00" in console
    assert "$750.00" in console
    assert "$5,000.00" not in console

    refused = client.post(
        "/configure/preset",
        data={"program_id": "1", "name": "Weekend", "preset": "Returns closed"},
        follow_redirects=True,
    )
    assert "refuses changes to the Policy Preset" in refused.text
    assert client.get("/").text.count("$10,000.00") >= 1
    assert "$5,000.00" not in client.get("/").text

    audit = client.get("/audit").text
    assert audit.count("Policy Preset applied") == 1
    assert "Hackathon weekend" in audit

    unknown = client.post(
        "/configure/preset", data={"name": "Other", "preset": "Not a preset"}
    )
    assert unknown.status_code == 400
