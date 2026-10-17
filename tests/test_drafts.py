"""Admin drafts propose, confirm, and abandon at the money seam."""

import re
import time

import pytest
from fastapi.testclient import TestClient

from creditpool import db, drafts, money
from creditpool.app import create_app
from creditpool.money import Status
from test_money import World

BUDGET = 10_000
CAP = 5_000
NOW = 1_800_000_000.0


@pytest.fixture
def world(tmp_path):
    path = str(tmp_path / "creditpool.db")
    conn = db.connect(path)
    db.init_schema(conn)
    program_id = money.create_program(conn, "Hackathon", BUDGET, CAP, NOW + 3600, now=NOW)
    alpha = money.add_team(conn, program_id, "Alpha")
    beta = money.add_team(conn, program_id, "Beta")
    yield World(path, conn, program_id, money.pool_team_id(conn, program_id), alpha, beta)
    conn.close()


def ledger_rows(conn):
    return [
        tuple(row) for row in conn.execute(
            "SELECT team_id, amount_cents, kind, operation_id FROM issuer_ledger ORDER BY id"
        )
    ]


def counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("operations", "offers", "trades", "reservations")
    )


def steps(conn, program_id):
    return [
        row["step"] for row in conn.execute(
            "SELECT step FROM program_audit_events WHERE program_id = ? ORDER BY id",
            (program_id,),
        )
    ]


def propose(world, instruction, proposer=None):
    return drafts.propose(
        world.conn, world.program_id, instruction, proposer or drafts.StubProposer(), now=NOW,
    )


class Scripted:
    model = "script"

    def __init__(self, raw):
        self.raw = raw

    def complete(self, messages):
        return self.raw


def test_parse_alone_does_not_write(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    before_ledger, before_counts = ledger_rows(world.conn), counts(world.conn)
    raw = drafts.StubProposer().complete(
        [{"role": "user", "content": "Instruction:\nreturn 10 from Alpha"}]
    )

    draft = drafts.interpret(world.conn, world.program_id, "return 10 from Alpha", raw, now=NOW)

    assert isinstance(draft, drafts.Draft)
    assert draft.kind is drafts.Kind.RETURN
    assert draft.amount_cents == 1_000
    assert ledger_rows(world.conn) == before_ledger
    assert counts(world.conn) == before_counts
    assert steps(world.conn, world.program_id) == []


def test_propose_return_is_a_draft_and_leaves_the_ledger(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    before_ledger, before_counts = ledger_rows(world.conn), counts(world.conn)
    fingerprint = money.ledger_fingerprint(world.conn, world.program_id)

    draft = propose(world, "Please return $10.00 from Alpha.")

    assert isinstance(draft, drafts.Draft)
    assert draft.kind is drafts.Kind.RETURN
    assert (draft.from_team_id, draft.to_team_id, draft.amount_cents) == (world.a, world.pool, 1_000)
    assert draft.from_name == "Alpha"
    assert draft.to_name == "Program Pool"
    assert ledger_rows(world.conn) == before_ledger
    assert counts(world.conn) == before_counts
    assert money.ledger_fingerprint(world.conn, world.program_id).digest == fingerprint.digest
    assert steps(world.conn, world.program_id) == ["nl_proposed"]


def test_confirm_return_queues_one_operation_and_process_moves_credit_once(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    draft = propose(world, "return 10 from Alpha")
    before_confirm = ledger_rows(world.conn)

    confirmed = drafts.confirm(world.conn, draft, "ret-1", now=NOW)
    again = drafts.confirm(world.conn, draft, "ret-1", now=NOW)

    assert confirmed.operation_id == again.operation_id
    assert ledger_rows(world.conn) == before_confirm
    assert counts(world.conn) == (1, 0, 0, 0)
    op = money.get_operation(world.conn, confirmed.operation_id)
    assert op.type == "return"
    assert (op.from_team_id, op.to_team_id, op.amount_cents) == (world.a, world.pool, 1_000)
    assert op.status is Status.REQUESTED
    assert steps(world.conn, world.program_id).count("nl_confirmed") == 1

    done = money.process_operation(world.conn, op.id, now=NOW)
    money.process_operation(world.conn, op.id, now=NOW)

    assert done.status is Status.RECONCILED
    assert money.ledger_balance(world.conn, world.a) == 2_000
    assert money.ledger_balance(world.conn, world.pool) == 1_000


def test_abandon_leaves_balances_and_operations(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    draft = propose(world, "return 10 from Alpha")
    before_ledger, before_counts = ledger_rows(world.conn), counts(world.conn)

    drafts.abandon(world.conn, draft, "gone", now=NOW)
    drafts.abandon(world.conn, draft, "gone", now=NOW)

    assert ledger_rows(world.conn) == before_ledger
    assert counts(world.conn) == before_counts
    assert steps(world.conn, world.program_id) == ["nl_proposed", "nl_abandoned"]


def test_request_reallocate_offer_and_accept_confirm_through_the_form_apis(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    money.grant(world.conn, world.pool, 2_000, now=NOW)
    before_moves = ledger_rows(world.conn)

    request = propose(world, "request 5 for Beta")
    assert isinstance(request, drafts.Draft)
    assert (request.from_team_id, request.to_team_id, request.amount_cents) == (world.pool, world.b, 500)
    requested = drafts.confirm(world.conn, request, "req-1", now=NOW)
    assert money.get_operation(world.conn, requested.operation_id).type == "reallocate"
    assert ledger_rows(world.conn) == before_moves

    move = propose(world, "reallocate 12 from Alpha to Beta")
    assert isinstance(move, drafts.Draft)
    assert (move.from_team_id, move.to_team_id, move.amount_cents) == (world.a, world.b, 1_200)
    moved = drafts.confirm(world.conn, move, "move-1", now=NOW)
    assert money.get_operation(world.conn, moved.operation_id).type == "reallocate"
    assert ledger_rows(world.conn) == before_moves

    offer = propose(world, "offer 10 from Alpha for 4 note outside")
    assert isinstance(offer, drafts.Draft)
    assert offer.kind is drafts.Kind.OFFER
    assert (offer.amount_cents, offer.settlement_ask_cents, offer.note) == (1_000, 400, "outside")
    posted = drafts.confirm(world.conn, offer, "off-1", now=NOW)
    assert counts(world.conn)[0] == 2
    assert counts(world.conn)[1] == 1
    assert money.get_offer(world.conn, posted.offer_id).status is money.OfferStatus.OPEN
    assert ledger_rows(world.conn) == before_moves

    accept = propose(world, f"accept offer {posted.offer_id} as Beta")
    assert isinstance(accept, drafts.Draft)
    assert accept.kind is drafts.Kind.ACCEPT
    assert (accept.from_team_id, accept.to_team_id, accept.amount_cents) == (world.a, world.b, 1_000)
    trade = drafts.confirm(world.conn, accept, "acc-1", now=NOW)
    assert ledger_rows(world.conn) == before_moves
    assert counts(world.conn)[:3] == (3, 1, 1)
    op = money.get_operation(world.conn, trade.operation_id)
    assert op.type == "reallocate"
    assert op.status is Status.REQUESTED
    assert (op.from_team_id, op.to_team_id, op.amount_cents) == (world.a, world.b, 1_000)

    done = money.process_operation(world.conn, trade.operation_id, now=NOW)
    assert done.status is Status.RECONCILED
    assert money.ledger_balance(world.conn, world.a) == 2_000
    assert money.ledger_balance(world.conn, world.b) == 1_000


def test_rule_refusals_explain_and_do_not_write_credit(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    money.add_team(world.conn, world.program_id, "Closed", eligible=False)
    before_ledger, before_counts = ledger_rows(world.conn), counts(world.conn)

    cases = [
        ("reallocate 10 from Alpha to Closed", "ineligible", "recipient Team is not eligible"),
        ("return 100 from Alpha", "insufficient_balance", "source ledger balance too low"),
        ("request 5 for Beta", "insufficient_balance", "source ledger balance too low"),
        ("return 10 from Gamma", "unknown_team", "No Team named Gamma in this Program."),
        ("send everything", "unreadable", "did not name a Return"),
    ]
    for instruction, rule, detail in cases:
        result = propose(world, instruction)
        assert isinstance(result, drafts.Refusal), instruction
        assert result.rule == rule
        assert detail in result.detail
        assert ledger_rows(world.conn) == before_ledger
        assert counts(world.conn) == before_counts

    money.grant(world.conn, world.b, CAP, now=NOW)
    capped = propose(world, "reallocate 1 from Alpha to Beta")
    assert isinstance(capped, drafts.Refusal)
    assert capped.rule == "cap"
    assert "per-Team cap" in capped.detail

    expired = drafts.propose(
        world.conn, world.program_id, "return 1 from Alpha", drafts.StubProposer(), now=NOW + 3600,
    )
    assert isinstance(expired, drafts.Refusal)
    assert expired.rule == "expired"

    denied = money.create_program(
        world.conn, "Closed Program", BUDGET, CAP, NOW + 3600, returns_allowed=False, now=NOW,
    )
    team = money.add_team(world.conn, denied, "Alpha")
    money.grant(world.conn, team, 3_000, now=NOW)
    blocked = drafts.propose(world.conn, denied, "return 1 from Alpha", drafts.StubProposer(), now=NOW)
    assert isinstance(blocked, drafts.Refusal)
    assert blocked.rule == "returns_disabled"
    assert "Returns" in blocked.detail

    before_confirm = ledger_rows(world.conn)
    crafted = drafts.Draft(
        kind=drafts.Kind.RETURN, program_id=world.program_id, instruction="return 100 from Alpha",
        from_team_id=world.a, to_team_id=world.pool, from_name="Alpha", to_name="Program Pool",
        amount_cents=10_000,
    )
    refused = drafts.confirm(world.conn, crafted, "nope", now=NOW)
    assert isinstance(refused, drafts.Refusal)
    assert refused.rule == "insufficient_balance"
    assert ledger_rows(world.conn) == before_confirm
    assert counts(world.conn) == before_counts
    assert "nl_confirmed" not in steps(world.conn, world.program_id)


def test_held_available_balance_is_refused_without_another_operation(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    op = money.create_return(world.conn, world.program_id, world.a, 3_000, "hold", now=NOW)
    assert money.reserve_funds(world.conn, op.id, now=NOW).ok
    before_ledger, before_counts = ledger_rows(world.conn), counts(world.conn)

    result = propose(world, "return 10 from Alpha")

    assert isinstance(result, drafts.Refusal)
    assert result.rule == "insufficient_available"
    assert ledger_rows(world.conn) == before_ledger
    assert counts(world.conn) == before_counts


def test_offer_over_balance_and_accept_over_cap_do_not_write(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    money.grant(world.conn, world.b, CAP, now=NOW)
    before = (ledger_rows(world.conn), counts(world.conn))

    too_big = propose(world, "offer 100 from Alpha for 4")
    assert isinstance(too_big, drafts.Refusal)
    assert too_big.rule == "insufficient_balance"
    assert (ledger_rows(world.conn), counts(world.conn)) == before

    posted = drafts.confirm(world.conn, propose(world, "offer 10 from Alpha for 4"), "off-1", now=NOW)
    accept = propose(world, f"accept offer {posted.offer_id} as Beta")
    assert isinstance(accept, drafts.Refusal)
    assert accept.rule == "cap"
    assert money.get_offer(world.conn, posted.offer_id).status is money.OfferStatus.OPEN
    assert counts(world.conn)[2] == 0
    assert ledger_rows(world.conn) == before[0]


def test_flagged_resolution_confirms_without_moving_credit(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    closed = money.add_team(world.conn, world.program_id, "Closed", eligible=False)
    first = money.create_operation(
        world.conn, program_id=world.program_id, type="reallocate",
        from_team_id=world.a, to_team_id=closed, amount_cents=100,
        idempotency_key="flag-1", now=NOW,
    )
    second = money.create_operation(
        world.conn, program_id=world.program_id, type="reallocate",
        from_team_id=world.a, to_team_id=closed, amount_cents=100,
        idempotency_key="flag-2", now=NOW,
    )
    assert money.process_operation(world.conn, first.id, now=NOW).status is Status.FLAGGED
    assert money.process_operation(world.conn, second.id, now=NOW).status is Status.FLAGGED
    before_ledger = ledger_rows(world.conn)

    retry = propose(world, f"retry flagged {first.id}")
    assert isinstance(retry, drafts.Draft)
    assert retry.action == "retry"
    assert money.get_operation(world.conn, first.id).status is Status.FLAGGED
    assert ledger_rows(world.conn) == before_ledger

    done = drafts.confirm(world.conn, retry, "retry-1", now=NOW)
    assert money.get_operation(world.conn, done.operation_id).status is Status.REQUESTED
    assert ledger_rows(world.conn) == before_ledger

    reject = propose(world, f"reject flagged {second.id} because cap")
    assert isinstance(reject, drafts.Draft)
    assert reject.reason == "cap"
    drafts.confirm(world.conn, reject, "reject-1", now=NOW)
    assert money.get_operation(world.conn, second.id).status is Status.FAILED
    assert ledger_rows(world.conn) == before_ledger

    missing = propose(world, "retry flagged 99999")
    assert isinstance(missing, drafts.Refusal)
    assert missing.rule == "unknown_operation"
    settled = money.process_operation(
        world.conn,
        money.create_return(world.conn, world.program_id, world.a, 100, "done", now=NOW).id,
        now=NOW,
    )
    after_settle = ledger_rows(world.conn)
    not_flagged = propose(world, f"retry flagged {settled.id}")
    assert isinstance(not_flagged, drafts.Refusal)
    assert not_flagged.rule == "not_flagged"
    assert ledger_rows(world.conn) == after_settle


def test_scripted_json_uses_the_same_draft_path(world):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    raw = '```json\n{"kind":"return","team":"Alpha","amount":"10.00"}\n```'

    draft = propose(world, "whatever the model heard", Scripted(raw))

    assert isinstance(draft, drafts.Draft)
    assert draft.kind is drafts.Kind.RETURN
    assert draft.amount_cents == 1_000
    assert counts(world.conn) == (0, 0, 0, 0)


def test_propose_traces_the_model_call(world, monkeypatch):
    money.grant(world.conn, world.a, 3_000, now=NOW)
    seen = []
    monkeypatch.setattr("creditpool.prism.trace_llm_call", lambda **kwargs: seen.append(kwargs))
    monkeypatch.setattr("creditpool.prism.submit_run", lambda **kwargs: seen.append(kwargs) or None)

    propose(world, "return 10 from Alpha")

    llm = next(item for item in seen if "input_messages" in item)
    assert "return 10 from Alpha" in llm["input_messages"][-1]["content"]
    assert llm["model"] == "creditpool-stub"
    assert llm["session_id"] == f"creditpool-nl-{world.program_id}"
    assert llm["agent_version"] == "nl"
    run = next(item for item in seen if "spans" in item)
    assert run["outcome"] == "proposed"
    assert run["spans"][0]["name"] == "propose"


def test_default_proposer_is_the_stub_without_a_key(monkeypatch):
    monkeypatch.delenv("NICRRON_API_KEY", raising=False)
    assert isinstance(drafts.default_proposer(), drafts.StubProposer)


def _form(html, action):
    match = re.search(rf'<form method="post" action="{action}">(.*?)</form>', html, re.S)
    assert match, action
    return dict(re.findall(r'name="([^"]+)" value="([^"]*)"', match.group(1)))


def test_admin_page_proposes_confirms_and_abandons(tmp_path):
    path = str(tmp_path / "ui.db")
    conn = db.connect(path)
    db.init_schema(conn)
    program_id = money.create_program(conn, "Hackathon", BUDGET, CAP, time.time() + 86_400)
    alpha = money.add_team(conn, program_id, "Alpha")
    money.add_team(conn, program_id, "Beta")
    money.grant(conn, alpha, 3_000)
    conn.close()
    client = TestClient(create_app(path))

    home = client.get("/")
    assert home.status_code == 200
    assert 'action="/admin/propose"' in home.text
    assert "return 25 from Alpha" in home.text

    proposed = client.post(
        "/admin/propose", data={"program_id": program_id, "instruction": "return 10 from Alpha"},
    )
    assert proposed.status_code == 200
    assert "Return" in proposed.text
    assert "Alpha" in proposed.text
    assert "$10.00" in proposed.text
    assert 'action="/admin/confirm"' in proposed.text
    conn = db.connect(path)
    assert money.ledger_balance(conn, alpha) == 3_000
    assert counts(conn) == (0, 0, 0, 0)
    assert "nl_proposed" in client.get("/audit").text
    assert "nl_confirmed" not in client.get("/audit").text

    edited = client.post(
        "/admin/propose",
        data={"program_id": program_id, "instruction": "return 1 from Alpha"},
    )
    assert edited.status_code == 200
    assert "$1.00" in edited.text
    assert counts(conn) == (0, 0, 0, 0)

    abandoned = client.post("/admin/abandon", data=_form(edited.text, "/admin/abandon"), follow_redirects=True)
    assert "Draft abandoned" in abandoned.text
    assert counts(conn) == (0, 0, 0, 0)
    assert money.ledger_balance(conn, alpha) == 3_000
    assert "nl_abandoned" in client.get("/audit").text
    assert "nl_confirmed" not in client.get("/audit").text

    proposed = client.post(
        "/admin/propose", data={"program_id": program_id, "instruction": "return 10 from Alpha"},
    )
    fields = _form(proposed.text, "/admin/confirm")
    confirmed = client.post("/admin/confirm", data=fields, follow_redirects=True)
    assert "Return queued as Operation #" in confirmed.text
    assert counts(conn) == (1, 0, 0, 0)
    assert money.ledger_balance(conn, alpha) == 3_000
    again = client.post("/admin/confirm", data=fields, follow_redirects=True)
    assert again.status_code == 200
    assert counts(conn) == (1, 0, 0, 0)
    assert "nl_confirmed" in client.get("/audit").text

    refused = client.post(
        "/admin/propose", data={"program_id": program_id, "instruction": "return 100 from Alpha"},
    )
    assert "source ledger balance too low" in refused.text
    assert 'action="/admin/confirm"' not in refused.text
    assert counts(conn) == (1, 0, 0, 0)
    assert money.ledger_balance(conn, alpha) == 3_000
    conn.close()
