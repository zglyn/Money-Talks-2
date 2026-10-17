"""Agent worker: an LLM sequences tool calls, the tools and Issuer Ledger enforce money (ADR-0002).

`run_agent` drives one Operation through an OpenAI-compatible chat model. `MockLLM` is the
default and needs no network. The Nicrron API (NICRRON_API_KEY) is used only for reasoning,
never as the Issuer Ledger.
"""

import dataclasses
import importlib
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from creditpool import env, money, prism
from creditpool.claim import claim_operations
from creditpool.money import Status

env.load()

log = logging.getLogger(__name__)

MAX_STEPS = 20
NICRRON_BASE_URL = "https://api.nicrron.ai/v1"
ROOT_CAUSE = (
    "v1 applies balance changes without holding a Reservation, so concurrent or oversized "
    "requests spend money the Team does not have."
)

V1_PROMPT = "You process one CreditPool Operation. Use the tools to apply it."
V2_PROMPT = (
    "You process one CreditPool Operation. Money is enforced by the tools, never by you.\n"
    "Call tools in this order and stop when an Operation is flagged or reconciled:\n"
    "1. get_operation\n"
    "2. get_program_rules\n"
    "3. get_team_balance for the source Team\n"
    "4. check_eligibility\n"
    "5. If check_eligibility fails: flag_for_admin, then write_audit_event, then stop.\n"
    "6. reserve_funds\n"
    "7. apply_reallocation\n"
    "8. reconcile\n"
    "9. write_audit_event\n"
    "If any step reports ok=false, stop and write_audit_event."
)


def _spec(name: str, description: str, **params: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {k: {"type": v} for k, v in params.items()},
                "required": list(params),
            },
        },
    }


TOOL_SPECS = [
    _spec("get_operation", "Read the Operation.", operation_id="integer"),
    _spec("get_program_rules", "Read Program Rules for a Program.", program_id="integer"),
    _spec("get_team_balance", "Ledger and Available Balance of a Team.", team_id="integer"),
    _spec(
        "check_eligibility",
        "Check Program Rules for this Operation without moving Credit.",
        operation_id="integer",
    ),
    _spec("reserve_funds", "Hold the Operation amount on the source Team.", operation_id="integer"),
    _spec("apply_reallocation", "Apply the transfer on the Issuer Ledger.", operation_id="integer"),
    _spec("reconcile", "Verify Issuer Ledger, Operation, and Reservations agree.", operation_id="integer"),
    _spec(
        "write_audit_event",
        "Append an Audit Event.",
        operation_id="integer", step="string", detail="string",
    ),
    _spec(
        "flag_for_admin",
        "Pause the Operation for Admin review.",
        operation_id="integer", reason="string",
    ),
]


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class ToolBox:
    """The nine agent tools, bound to one Operation. The model cannot touch another one."""

    def __init__(self, conn: sqlite3.Connection, operation_id: int) -> None:
        self.conn = conn
        self.operation_id = operation_id

    def call(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        if "operation_id" in args and args["operation_id"] != self.operation_id:
            return {"error": "wrong_operation", "detail": "this run owns one Operation"}
        try:
            return handler(args)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _op(self) -> money.Operation:
        return money.get_operation(self.conn, self.operation_id)

    def tool_get_operation(self, args: dict) -> dict:
        return _plain(self._op())

    def tool_get_program_rules(self, args: dict) -> dict:
        row = self.conn.execute(
            "SELECT * FROM programs WHERE id = ?", (args["program_id"],)
        ).fetchone()
        if row is None:
            return {"error": "unknown_program"}
        return {
            "program_id": row["id"],
            "name": row["name"],
            "total_budget_cents": row["total_budget_cents"],
            "program_total_cents": money.program_total(self.conn, row["id"]),
            "per_team_cap_cents": row["per_team_cap_cents"],
            "expires_at": row["expires_at"],
            "expired": time.time() >= row["expires_at"],
            "returns_allowed": bool(row["returns_allowed"]),
            "pool_team_id": money.pool_team_id(self.conn, row["id"]),
        }

    def tool_get_team_balance(self, args: dict) -> dict:
        team_id = args["team_id"]
        return {
            "team_id": team_id,
            "ledger_cents": money.ledger_balance(self.conn, team_id),
            "available_cents": money.available_balance(self.conn, team_id),
        }

    def tool_check_eligibility(self, args: dict) -> dict:
        violation = money._program_rule_violation(self.conn, self._op(), time.time())
        if violation is None or violation[0] == "no_reservation":
            return {"ok": True}
        return {"ok": False, "rule": violation[0], "detail": violation[1]}

    def tool_reserve_funds(self, args: dict) -> dict:
        return _plain(money.reserve_funds(self.conn, self.operation_id))

    def tool_apply_reallocation(self, args: dict) -> dict:
        apply = (
            money.apply_reallocation_v1
            if self._op().agent_version == "v1"
            else money.apply_reallocation_v2
        )
        return _plain(apply(self.conn, self.operation_id))

    def tool_reconcile(self, args: dict) -> dict:
        return _plain(money.reconcile(self.conn, self.operation_id))

    def tool_write_audit_event(self, args: dict) -> dict:
        actor = f"agent-{self._op().agent_version}"
        money.write_audit_event(
            self.conn, self.operation_id, actor, args.get("step", "note"), args.get("detail", "")
        )
        return {"ok": True}

    def tool_flag_for_admin(self, args: dict) -> dict:
        return _plain(money.flag_for_admin(self.conn, self.operation_id, args.get("reason", "")))


class LLM(Protocol):
    """OpenAI chat-completions shape: return the assistant message dict."""

    def complete(self, messages: list[dict], tools: list[dict]) -> dict: ...


def _tool_call(index: int, name: str, args: dict) -> dict:
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _history(messages: list[dict]) -> list[tuple[str, dict, dict]]:
    """(tool name, args, result) for every executed tool call, in order."""
    calls: dict[str, tuple[str, dict]] = {}
    out: list[tuple[str, dict, dict]] = []
    for m in messages:
        for tc in m.get("tool_calls") or []:
            calls[tc["id"]] = (tc["function"]["name"], json.loads(tc["function"]["arguments"]))
        if m["role"] == "tool":
            name, args = calls[m["tool_call_id"]]
            out.append((name, args, json.loads(m["content"])))
    return out


class MockLLM:
    """Scripted agent with no network: v1 applies naively, v2 follows the ordered checks."""

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        operation_id = json.loads(messages[1]["content"])["operation_id"]
        history = _history(messages)
        step = self._next(operation_id, history)
        if step is None:
            return {"role": "assistant", "content": "Done."}
        name, args = step
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call(len(history), name, args)],
        }

    def _next(self, op_id: int, history: list[tuple[str, dict, dict]]) -> tuple[str, dict] | None:
        results = {name: result for name, _, result in history}
        if not history:
            return "get_operation", {"operation_id": op_id}
        op = results["get_operation"]
        if "error" in op:
            return None
        plan = self._v1_plan if op["agent_version"] == "v1" else self._v2_plan
        for name, args, proceed in plan(op, results):
            if name in results:
                if not proceed(results[name]):
                    return self._wrap_up(op_id, results)
                continue
            return name, args
        return None

    @staticmethod
    def _wrap_up(op_id: int, results: dict[str, dict]) -> tuple[str, dict] | None:
        if "write_audit_event" in results:
            return None
        return "write_audit_event", {
            "operation_id": op_id, "step": "stopped", "detail": "a step reported ok=false",
        }

    @staticmethod
    def _ok(result: dict) -> bool:
        return result.get("ok", False) and "error" not in result

    def _v1_plan(self, op: dict, results: dict):
        oid = {"operation_id": op["id"]}
        yield "apply_reallocation", oid, self._ok
        yield "reconcile", oid, self._ok
        yield "write_audit_event", {**oid, "step": "agent_done", "detail": "v1"}, lambda r: True

    def _v2_plan(self, op: dict, results: dict):
        oid = {"operation_id": op["id"]}
        yield "get_program_rules", {"program_id": op["program_id"]}, lambda r: "error" not in r
        yield "get_team_balance", {"team_id": op["from_team_id"]}, lambda r: "error" not in r
        yield "check_eligibility", oid, lambda r: True
        eligibility = results.get("check_eligibility")
        if eligibility is not None and not self._ok(eligibility):
            yield "flag_for_admin", {**oid, "reason": eligibility.get("rule", "ineligible")}, lambda r: True
            yield "write_audit_event", {**oid, "step": "flagged_by_agent", "detail": eligibility.get("detail", "")}, lambda r: True
            return
        yield "reserve_funds", oid, self._ok
        yield "apply_reallocation", oid, self._ok
        yield "reconcile", oid, self._ok
        yield "write_audit_event", {**oid, "step": "agent_done", "detail": "v2"}, lambda r: True


class NicrronLLM:
    """Nicrron API through the openai package (OpenAI-compatible)."""

    def __init__(self, api_key: str, base_url: str = NICRRON_BASE_URL, model: str = "nicrron/auto"):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools
        )
        return response.choices[0].message.model_dump(exclude_none=True)


def default_llm() -> LLM:
    """Real Nicrron when NICRRON_API_KEY is set and `openai` is installed, else MockLLM."""
    api_key = os.environ.get("NICRRON_API_KEY")
    if api_key:
        try:
            return NicrronLLM(
                api_key,
                os.environ.get("NICRRON_BASE_URL", NICRRON_BASE_URL),
                os.environ.get("NICRRON_MODEL", "nicrron/auto"),
            )
        except ImportError:
            log.warning("NICRRON_API_KEY is set but openai is not installed; using MockLLM")
    return MockLLM()


Exporter = Callable[[dict], "str | None"]


def exporter_from_env() -> Exporter | None:
    """PRISMTRACE_EXPORTER="module:callable" receives the session payload, returns a trace id.

    The prismtrace SDK is not a dependency, so the real submission lives behind this hook.
    """
    target = os.environ.get("PRISMTRACE_EXPORTER")
    if not target:
        return None
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


class PrismRecorder:
    """One PRISM session per Operation: root metadata, a span per tool call, the trajectory."""

    def __init__(self, operation_id: int, agent_version: str, exporter: Exporter | None = None):
        self.trace_id = str(uuid.uuid4())
        self.session = {"operation_id": operation_id, "agent_version": agent_version}
        self.spans: list[dict] = []
        self.trajectory: list[dict] = []
        self.exporter = exporter

    def message(self, message: dict) -> None:
        self.trajectory.append(message)

    def span(self, name: str, args: dict, result: dict, started: float) -> None:
        self.spans.append({
            "name": name, "input": args, "output": result,
            "error": "error" in result or result.get("ok") is False,
            "started_at": started, "ended_at": time.time(),
        })

    def finish(self, outcome: str) -> None:
        if self.exporter is None:
            return
        payload = {
            "trace_id": self.trace_id, "session": self.session, "outcome": outcome,
            "spans": self.spans, "trajectory": self.trajectory,
        }
        try:
            self.trace_id = self.exporter(payload) or self.trace_id
        except Exception:
            log.exception("PRISM export failed; keeping the local trace id")


@dataclass
class AgentRun:
    id: int
    operation_id: int
    agent_version: str
    outcome: str
    status: Status
    error_code: str | None
    prism: PrismRecorder
    steps: list[str] = field(default_factory=list)

    @property
    def prism_trace_id(self) -> str:
        return self.prism.trace_id

    @property
    def ok(self) -> bool:
        """A correctly Flagged Operation counts as a successful run; mismatches and errors do not."""
        return self.outcome in ("reconciled", "flagged")


_OUTCOMES = {Status.RECONCILED: "reconciled", Status.FLAGGED: "flagged", Status.FAILED: "failed"}


def run_agent(
    conn: sqlite3.Connection,
    operation_id: int,
    llm: LLM | None = None,
    exporter: Exporter | None = None,
) -> AgentRun:
    """Run one Operation through the agent loop and record the run in `agent_runs`."""
    llm = default_llm() if llm is None else llm
    version = money.get_operation(conn, operation_id).agent_version
    model_name = os.environ.get("NICRRON_MODEL", "nicrron/auto") if isinstance(llm, NicrronLLM) else "mock-llm"
    recorder = PrismRecorder(operation_id, version, exporter or exporter_from_env())
    tools = ToolBox(conn, operation_id)
    messages: list[dict] = [
        {"role": "system", "content": V1_PROMPT if version == "v1" else V2_PROMPT},
        {"role": "user", "content": json.dumps({"operation_id": operation_id})},
    ]
    steps: list[str] = []
    error_code: str | None = None
    with prism.operation_session(operation_id) as session_id:
        for _ in range(MAX_STEPS):
            started = time.time()
            try:
                reply = llm.complete(messages, TOOL_SPECS)
            except Exception as exc:
                error_code = f"llm_error:{type(exc).__name__}"
                break
            latency_ms = int((time.time() - started) * 1000)
            messages.append(reply)
            recorder.message(reply)
            prism.trace_llm_call(
                session_id=session_id,
                agent_version=version,
                model=model_name,
                input_messages=messages[:-1],
                output_message=reply,
                latency_ms=latency_ms,
            )
            calls = reply.get("tool_calls") or []
            if not calls:
                break
            for tc in calls:
                name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"] or "{}")
                tool_started = time.time()
                result = tools.call(name, args)
                recorder.span(name, args, result, tool_started)
                steps.append(name)
                if "error" in result and error_code is None:
                    error_code = f"tool_error:{name}"
                tool_message = {
                    "role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result),
                }
                messages.append(tool_message)
                recorder.message(tool_message)

        op = money.get_operation(conn, operation_id)
        outcome = "error" if error_code and error_code.startswith("llm_error") else _OUTCOMES.get(
            op.status, "incomplete"
        )
        if error_code is None and op.status in (Status.FAILED, Status.FLAGGED):
            error_code = op.flag_reason
        recorder.finish(outcome)
        live_id = prism.submit_run(
            session_id=session_id,
            agent_version=version,
            model=model_name,
            spans=recorder.spans,
            outcome=outcome,
        )
        if live_id:
            recorder.trace_id = live_id

    cur = conn.execute(
        "INSERT INTO agent_runs (operation_id, agent_version, outcome, tool_calls, error_code,"
        " prism_trace_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            operation_id, version, outcome,
            json.dumps([{"name": s["name"], "error": s["error"]} for s in recorder.spans]),
            error_code, recorder.trace_id, time.time(),
        ),
    )
    return AgentRun(
        cur.lastrowid, operation_id, version, outcome, op.status, error_code, recorder, steps,
    )


def process_claimed(
    conn: sqlite3.Connection,
    operation_ids: list[int],
    llm: LLM | None = None,
    exporter: Exporter | None = None,
) -> list[AgentRun]:
    return [run_agent(conn, op_id, llm, exporter) for op_id in operation_ids]


def claim_and_process_loop(
    conn: sqlite3.Connection,
    worker_id: str,
    llm: LLM | None = None,
    batch_size: int = 10,
    exporter: Exporter | None = None,
) -> list[AgentRun]:
    """Claim and process requested Operations until none are left. One-shot batch."""
    runs: list[AgentRun] = []
    while claimed := claim_operations(conn, batch_size, worker_id):
        runs += process_claimed(conn, claimed, llm, exporter)
    return runs
