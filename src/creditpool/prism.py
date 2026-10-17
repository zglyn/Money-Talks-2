"""PRISM live tracing. Credentials come only from the environment (never from source)."""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

from creditpool import env

log = logging.getLogger(__name__)

_OK_OUTCOMES = frozenset({"reconciled", "flagged", "proposed", "refused"})
_CLIENT: Any = None
_CLIENT_TRIED = False


def configured() -> bool:
    env.load()
    return bool(
        os.environ.get("PRISMTRACE_API_KEY")
        and os.environ.get("PRISMTRACE_PROJECT_ID")
        and os.environ.get("PRISMTRACE_HOST")
    )


def client():
    """Return a shared `PRISMtrace` client, or None if env / SDK is missing."""
    global _CLIENT, _CLIENT_TRIED
    env.load()
    if _CLIENT_TRIED:
        return _CLIENT
    _CLIENT_TRIED = True
    api_key = os.environ.get("PRISMTRACE_API_KEY")
    project_id = os.environ.get("PRISMTRACE_PROJECT_ID")
    host = os.environ.get("PRISMTRACE_HOST")
    if not (api_key and project_id and host):
        return None
    try:
        from prismtrace import PRISMtrace
    except ImportError:
        log.warning("PRISMTRACE_* set but prismtrace-sdk is not installed; pip install -e \".[prism]\"")
        return None
    _CLIENT = PRISMtrace(api_key=api_key, host=host, project_id=project_id)
    return _CLIENT


@contextmanager
def session(session_id: str) -> Iterator[str]:
    """Group PRISM events under one session id. No-op when the client is missing."""
    pt = client()
    if pt is None:
        yield session_id
        return
    import prismtrace

    with prismtrace.session(session_id):
        yield session_id


@contextmanager
def operation_session(operation_id: int) -> Iterator[str]:
    """Group all PRISM events for one Operation under one session_id."""
    with session(f"creditpool-op-{operation_id}") as session_id:
        yield session_id


def trace_llm_call(
    *,
    session_id: str,
    agent_version: str,
    model: str,
    input_messages: list[dict],
    output_message: dict | str,
    latency_ms: int,
    token_count_input: int = 0,
    token_count_output: int = 0,
) -> None:
    pt = client()
    if pt is None:
        return
    if isinstance(output_message, dict):
        output = output_message.get("content") or json_dumps(output_message)
    else:
        output = str(output_message)
    try:
        pt.trace_llm(
            model=model or "creditpool-agent",
            input_messages=_sanitize_messages(input_messages),
            output=output or "",
            latency_ms=max(latency_ms, 0),
            token_count_input=token_count_input,
            token_count_output=token_count_output,
            agent_name=f"creditpool-{agent_version}",
            agent_id=f"creditpool-{agent_version}",
            session_id=session_id,
            metadata={"operation_session": session_id},
        )
    except Exception:
        log.exception("PRISM trace_llm failed")


def submit_run(
    *,
    session_id: str,
    agent_version: str,
    model: str,
    spans: list[dict],
    outcome: str,
) -> str | None:
    """Submit a trajectory for the Operation. Returns a trajectory / trace id when available."""
    pt = client()
    if pt is None:
        return None
    steps = []
    for span in spans:
        err = bool(span.get("error"))
        started = span.get("started_at") or time.time()
        ended = span.get("ended_at") or started
        steps.append({
            "step_type": "tool_call",
            "label": span.get("name", "tool"),
            "tool_name": span.get("name", "tool"),
            "input_summary": _clip(span.get("input")),
            "output_summary": _clip(span.get("output")),
            "duration_ms": int(max(0.0, (ended - started) * 1000)),
            "status": "error" if err else "success",
        })
    try:
        resp = pt.submit_trajectory(
            steps,
            agent_name=f"creditpool-{agent_version}",
            agent_id=f"creditpool-{agent_version}",
            conversation_id=session_id,
            model=model or "creditpool-agent",
            final_status="success" if outcome in _OK_OUTCOMES else "error",
        )
    except Exception:
        log.exception("PRISM submit_trajectory failed")
        return None
    if not resp:
        return None
    return str(resp.get("id") or resp.get("trajectory_id") or session_id)


def _sanitize_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if content is None and m.get("tool_calls"):
            content = json_dumps({"tool_calls": m.get("tool_calls")})
        out.append({"role": role, "content": content or ""})
    return out


def _clip(value: Any, limit: int = 500) -> str:
    text = json_dumps(value) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 3] + "..."


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
