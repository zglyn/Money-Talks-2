"""Behavior tests for claiming and the agent worker, using MockLLM (no network)."""

import json
import threading
import time

import pytest

from creditpool import db, money
from creditpool.claim import claim_operations, release_stale_claims
from creditpool.money import Status
from creditpool.worker import (
    MockLLM, ToolBox, claim_and_process_loop, default_llm, run_agent,
)

CAP = 60_000


@pytest.fixture
def world(tmp_path):
    path = str(tmp_path / "creditpool.db")
    conn = db.connect(path)
    db.init_schema(conn)
    now = time.time()
    program_id = money.create_program(conn, "Hackathon", 200_000, CAP, now + 3600, now=now)
    teams = {n: money.add_team(conn, program_id, n) for n in ("a", "b", "c")}
    teams["out"] = money.add_team(conn, program_id, "out", eligible=False)
    for team in ("a", "b", "c"):
        money.grant(conn, teams[team], 20_000, now=now)
    yield conn, path, program_id, teams
    conn.close()


def reallocate(conn, program_id, src, dst, amount, key, version="v2"):
    return money.create_operation(
        conn, program_id=program_id, type="reallocate", from_team_id=src, to_team_id=dst,
        amount_cents=amount, idempotency_key=key, agent_version=version,
    )


def test_v2_run_is_reserved_applied_reconciled_in_six_or_more_steps(world):
    conn, _, pid, t = world
    op = reallocate(conn, pid, t["a"], t["b"], 5_000, "k1")

    run = run_agent(conn, op.id, MockLLM())

    assert run.outcome == "reconciled" and run.ok
    assert run.steps[:2] == ["get_operation", "get_program_rules"]
    assert len(run.steps) >= 6
    assert run.steps.index("reserve_funds") < run.steps.index("apply_reallocation")
    assert money.get_operation(conn, op.id).status is Status.RECONCILED
    assert money.ledger_balance(conn, t["b"]) == 25_000
    row = conn.execute("SELECT * FROM agent_runs WHERE operation_id = ?", (op.id,)).fetchone()
    assert row["outcome"] == "reconciled" and row["agent_version"] == "v2"
    assert row["prism_trace_id"] == run.prism_trace_id
    assert [c["name"] for c in json.loads(row["tool_calls"])] == run.steps
    assert [s["name"] for s in run.prism.spans] == run.steps
    assert run.prism.session == {"operation_id": op.id, "agent_version": "v2"}


def test_v2_flags_an_ineligible_recipient_and_that_counts_as_success(world):
    conn, _, pid, t = world
    op = reallocate(conn, pid, t["a"], t["out"], 1_000, "k1")

    run = run_agent(conn, op.id, MockLLM())

    assert run.outcome == "flagged" and run.ok and run.error_code == "ineligible"
    assert len(run.steps) >= 6
    assert "apply_reallocation" not in run.steps
    assert money.ledger_balance(conn, t["out"]) == 0
    assert "flagged_by_agent" in money.audit_steps(conn, op.id)


def test_v2_flags_the_second_of_two_drains_and_balance_stays_non_negative(world):
    conn, _, pid, t = world
    first = reallocate(conn, pid, t["a"], t["b"], 15_000, "d1")
    second = reallocate(conn, pid, t["a"], t["c"], 15_000, "d2")

    runs = [run_agent(conn, first.id), run_agent(conn, second.id)]

    assert [r.outcome for r in runs] == ["reconciled", "flagged"]
    assert money.ledger_balance(conn, t["a"]) == 5_000


def test_v1_naive_apply_double_spends_and_reconcile_catches_it(world):
    conn, _, pid, t = world
    first = reallocate(conn, pid, t["a"], t["b"], 15_000, "d1", "v1")
    second = reallocate(conn, pid, t["a"], t["c"], 15_000, "d2", "v1")

    runs = [run_agent(conn, first.id), run_agent(conn, second.id)]

    assert runs[0].outcome == "reconciled"
    assert runs[1].outcome == "failed" and not runs[1].ok
    assert runs[1].error_code == "reconcile_mismatch"
    assert "reserve_funds" not in runs[1].steps
    assert money.ledger_balance(conn, t["a"]) == -10_000


def test_tools_refuse_to_touch_an_operation_the_run_does_not_own(world):
    conn, _, pid, t = world
    mine = reallocate(conn, pid, t["a"], t["b"], 1_000, "k1")
    other = reallocate(conn, pid, t["a"], t["c"], 1_000, "k2")

    result = ToolBox(conn, mine.id).call("apply_reallocation", {"operation_id": other.id})

    assert result["error"] == "wrong_operation"
    assert money.get_operation(conn, other.id).status is Status.REQUESTED


def test_run_records_a_tool_error_without_raising(world):
    conn, _, pid, t = world
    op = reallocate(conn, pid, t["a"], t["b"], 1_000, "k1")

    class BadLLM:
        def complete(self, messages, tools):
            if len(messages) > 2:
                return {"role": "assistant", "content": "done"}
            call = {"name": "get_team_balance", "arguments": "{}"}
            return {"role": "assistant", "tool_calls": [{"id": "c0", "type": "function", "function": call}]}

    run = run_agent(conn, op.id, BadLLM())

    assert run.error_code == "tool_error:get_team_balance"
    assert run.outcome == "incomplete" and not run.ok


def test_claims_never_overlap_across_concurrent_workers(world):
    conn, path, pid, t = world
    for i in range(60):
        reallocate(conn, pid, t["a"], t["b"], 100, f"k{i}")
    claimed: dict[str, list[int]] = {}
    barrier = threading.Barrier(6)

    def work(name: str) -> None:
        own = db.connect(path)
        barrier.wait()
        mine: list[int] = []
        while ids := claim_operations(own, 4, name):
            mine += ids
        claimed[name] = mine
        own.close()

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    everything = [i for ids in claimed.values() for i in ids]
    assert len(everything) == len(set(everything)) == 60


def test_claimed_operations_are_skipped_until_the_claim_goes_stale(world):
    conn, _, pid, t = world
    op = reallocate(conn, pid, t["a"], t["b"], 100, "k1")

    assert claim_operations(conn, 5, "w1", now=1_000.0) == [op.id]
    assert claim_operations(conn, 5, "w2", now=1_001.0) == []
    assert release_stale_claims(conn, older_than_seconds=60, now=1_100.0) == 1
    assert claim_operations(conn, 5, "w2", now=1_101.0) == [op.id]


def test_claim_loop_processes_every_requested_operation_once(world):
    conn, _, pid, t = world
    ops = [reallocate(conn, pid, t["a"], t["b"], 500, f"k{i}") for i in range(5)]

    runs = claim_and_process_loop(conn, "w1", batch_size=2)

    assert sorted(r.operation_id for r in runs) == [o.id for o in ops]
    assert all(r.outcome == "reconciled" for r in runs)
    assert claim_and_process_loop(conn, "w1") == []
    assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 5


def test_default_llm_without_nicrron_key_is_mock(monkeypatch):
    monkeypatch.delenv("NICRRON_API_KEY", raising=False)
    assert isinstance(default_llm(), MockLLM)
