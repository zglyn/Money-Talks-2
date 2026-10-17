"""Admin drafts from a natural-language instruction.

Propose reads an instruction into a Draft or a Refusal. Confirm is the only step
that calls the Operation, Offer, and Flagged resolution APIs. Propose, refuse,
and abandon do not write the Issuer Ledger (ADR-0002).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Callable

from creditpool import money, prism

log = logging.getLogger(__name__)

NICRRON_BASE_URL = "https://api.nicrron.ai/v1"
MAX_INSTRUCTION = 2000
_UNREADABLE = (
    "The instruction did not name a Return, Request, Reallocate, Offer, Accept,"
    " or Flagged resolution."
)
_PROMPT = (
    "You turn one Admin instruction into one CreditPool draft. "
    "You do not move Credit. Reply with one JSON object and no other text.\n"
    "kind is return, request, reallocate, offer, accept, or resolve.\n"
    "return needs team and amount in USD.\n"
    "request needs team and amount in USD. Credit comes from the Program Pool.\n"
    "reallocate needs from_team, to_team, and amount in USD.\n"
    "offer needs team, amount in USD, settlement_ask in USD, and note.\n"
    "accept needs offer_id and to_team.\n"
    "resolve needs operation_id, action retry or reject, and reason.\n"
    "Use Team names and ids from the catalog. "
    'If the instruction is not one of those, reply {"kind":"none","detail":"..."}.'
)


class Kind(StrEnum):
    RETURN = "return"
    REQUEST = "request"
    REALLOCATE = "reallocate"
    OFFER = "offer"
    ACCEPT = "accept"
    RESOLVE = "resolve"


LABEL = {
    Kind.RETURN: "Return",
    Kind.REQUEST: "Request",
    Kind.REALLOCATE: "Reallocate",
    Kind.OFFER: "Offer",
    Kind.ACCEPT: "Accept",
    Kind.RESOLVE: "Flagged resolution",
}


@dataclass(frozen=True)
class Draft:
    kind: Kind
    program_id: int
    instruction: str
    from_team_id: int | None = None
    to_team_id: int | None = None
    from_name: str = ""
    to_name: str = ""
    amount_cents: int | None = None
    settlement_ask_cents: int | None = None
    offer_id: int | None = None
    operation_id: int | None = None
    action: str = ""
    reason: str = ""
    note: str = ""


@dataclass(frozen=True)
class Refusal:
    program_id: int
    instruction: str
    rule: str
    detail: str


@dataclass(frozen=True)
class Confirmed:
    kind: Kind
    program_id: int
    message: str
    operation_id: int | None = None
    offer_id: int | None = None
    trade_id: int | None = None


class StubProposer:
    """Deterministic reader for tests and for a demo with no Nicrron API key."""

    model = "creditpool-stub"

    def complete(self, messages: list[dict]) -> str:
        return _stub_json(_instruction_of(messages))


class NicrronProposer:
    """Live Nicrron completion. Optional. The stub is the default."""

    def __init__(self, api_key: str, base_url: str = NICRRON_BASE_URL, model: str = "nicrron/auto"):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def complete(self, messages: list[dict]) -> str:
        response = self.client.chat.completions.create(model=self.model, messages=messages)
        return response.choices[0].message.content or ""


def default_proposer() -> StubProposer | NicrronProposer:
    """Nicrron when NICRRON_API_KEY is set and openai is installed, otherwise the stub."""
    api_key = os.environ.get("NICRRON_API_KEY")
    if not api_key:
        return StubProposer()
    try:
        return NicrronProposer(
            api_key,
            os.environ.get("NICRRON_BASE_URL", NICRRON_BASE_URL),
            os.environ.get("NICRRON_MODEL", "nicrron/auto"),
        )
    except ImportError:
        log.warning("NICRRON_API_KEY is set but openai is not installed; using the draft stub")
        return StubProposer()


def propose(
    conn: sqlite3.Connection,
    program_id: int,
    instruction: str,
    proposer: StubProposer | NicrronProposer | None = None,
    now: float | None = None,
) -> Draft | Refusal:
    """Read an instruction into a Draft or a Refusal. Does not write the Issuer Ledger."""
    proposer = default_proposer() if proposer is None else proposer
    text = _clip(instruction)
    _require_program(conn, program_id)
    if not text:
        result: Draft | Refusal = Refusal(program_id, "", "unreadable", _UNREADABLE)
        _event(conn, program_id, "nl_refused", f"{result.rule}: {result.detail}", now)
        return result
    messages = _messages(conn, program_id, text)
    started = time.time()
    thrown: str | None = None
    try:
        raw = proposer.complete(messages)
    except Exception as exc:
        log.exception("draft model failed")
        raw = ""
        thrown = type(exc).__name__
    latency_ms = int((time.time() - started) * 1000)
    if thrown:
        result = Refusal(program_id, text, "model", "The model did not return a draft.")
        outcome = "error"
    else:
        result = interpret(conn, program_id, text, raw, now)
        outcome = "refused" if isinstance(result, Refusal) else "proposed"
    _trace(program_id, proposer.model, messages, raw or thrown or "", latency_ms, outcome, result)
    if isinstance(result, Draft):
        _event(conn, program_id, "nl_proposed", _draft_detail(result), now)
    else:
        _event(conn, program_id, "nl_refused", f"{result.rule}: {result.detail}", now)
    return result


def confirm(
    conn: sqlite3.Connection,
    draft: Draft,
    key: str,
    agent_version: str = "v2",
    now: float | None = None,
) -> Confirmed | Refusal:
    """Create or resolve through the same APIs as the forms. A refusal writes no Credit."""
    if not key.strip():
        raise ValueError("idempotency key is required")
    _require_program(conn, draft.program_id)
    refusal = preview(conn, draft, now)
    if refusal is not None:
        _event(conn, draft.program_id, "nl_refused", f"{refusal.rule}: {refusal.detail}", now)
        return refusal
    try:
        confirmed = _CONFIRM[draft.kind](conn, draft, key.strip(), agent_version, now)
    except (ValueError, LookupError) as exc:
        refusal = Refusal(draft.program_id, draft.instruction, "rejected", str(exc))
        _event(conn, draft.program_id, "nl_refused", f"{refusal.rule}: {refusal.detail}", now)
        return refusal
    detail = f"key={key.strip()}; {_confirmed_detail(confirmed)}"
    if not _seen(conn, draft.program_id, "nl_confirmed", key.strip()):
        _event(conn, draft.program_id, "nl_confirmed", detail, now)
    return confirmed


def abandon(
    conn: sqlite3.Connection,
    draft: Draft,
    key: str,
    now: float | None = None,
) -> None:
    """Record that the Admin left the draft. Does not write the Issuer Ledger."""
    if not key.strip():
        raise ValueError("idempotency key is required")
    _require_program(conn, draft.program_id)
    if _seen(conn, draft.program_id, "nl_abandoned", key.strip()):
        return
    _event(
        conn, draft.program_id, "nl_abandoned",
        f"key={key.strip()}; {_draft_detail(draft)}", now,
    )


def confirm_path(confirmed: Confirmed) -> str:
    if confirmed.kind in (Kind.OFFER, Kind.ACCEPT):
        return "/trades"
    return "/operations"


def form_values(draft: Draft) -> dict[str, str]:
    return {
        "kind": draft.kind.value,
        "program_id": str(draft.program_id),
        "instruction": draft.instruction,
        "from_team_id": _text(draft.from_team_id),
        "to_team_id": _text(draft.to_team_id),
        "from_name": draft.from_name,
        "to_name": draft.to_name,
        "amount_cents": _text(draft.amount_cents),
        "settlement_ask_cents": _text(draft.settlement_ask_cents),
        "offer_id": _text(draft.offer_id),
        "operation_id": _text(draft.operation_id),
        "action": draft.action,
        "reason": draft.reason,
        "note": draft.note,
    }


def draft_from_form(posted: dict[str, str]) -> Draft | Refusal:
    """Parse a confirm or abandon form into a Draft. Does not write."""
    program_id = _pos_int(posted.get("program_id"))
    if program_id is None:
        raise ValueError("program_id is required")
    instruction = _clip(posted.get("instruction") or "")
    try:
        kind = Kind(str(posted.get("kind") or "").strip())
    except ValueError:
        return Refusal(program_id, instruction, "incomplete", "The draft type is missing.")
    draft = Draft(
        kind=kind,
        program_id=program_id,
        instruction=instruction,
        from_team_id=_pos_int(posted.get("from_team_id")),
        to_team_id=_pos_int(posted.get("to_team_id")),
        amount_cents=_pos_int(posted.get("amount_cents")),
        settlement_ask_cents=_nonneg_int(posted.get("settlement_ask_cents")),
        offer_id=_pos_int(posted.get("offer_id")),
        operation_id=_pos_int(posted.get("operation_id")),
        action=str(posted.get("action") or "").strip().casefold(),
        reason=_clip(posted.get("reason") or "", 500),
        note=_clip(posted.get("note") or "", 500),
    )
    missing = _missing(draft)
    if missing:
        return Refusal(program_id, instruction, "incomplete", missing)
    return _with_names(posted, draft)


def interpret(
    conn: sqlite3.Connection,
    program_id: int,
    instruction: str,
    raw: str,
    now: float | None = None,
) -> Draft | Refusal:
    """Turn model JSON into a Draft or a Refusal. Does not write."""
    data = parse_model_text(raw)
    if data is None:
        return Refusal(program_id, instruction, "unreadable", "The model did not return a draft.")
    kind = str(data.get("kind") or "").strip().casefold()
    if kind in ("", "none", "null"):
        detail = str(data.get("detail") or "").strip() or _UNREADABLE
        return Refusal(program_id, instruction, "unreadable", detail[:240])
    binder = _BIND.get(kind)
    if binder is None:
        return Refusal(program_id, instruction, "unreadable", _UNREADABLE)
    return binder(conn, program_id, instruction, data, now)


def parse_model_text(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def preview(conn: sqlite3.Connection, draft: Draft, now: float | None = None) -> Refusal | None:
    """Program Rules the draft would hit. Does not write."""
    if draft.kind is Kind.RESOLVE:
        return _preview_resolve(conn, draft)
    if draft.kind is Kind.OFFER:
        return _preview_offer(conn, draft, now)
    if draft.kind is Kind.ACCEPT:
        return _preview_accept(conn, draft, now)
    return _preview_move(conn, draft, now)


def _preview_move(conn, draft: Draft, now: float | None) -> Refusal | None:
    missing = _missing(draft)
    if missing:
        return Refusal(draft.program_id, draft.instruction, "incomplete", missing)
    pool = money.pool_team_id(conn, draft.program_id)
    if draft.kind is Kind.RETURN:
        if draft.from_team_id == pool:
            return _pool_refusal(draft)
        op_type, source, dest, amount = "return", draft.from_team_id, pool, draft.amount_cents
    elif draft.kind is Kind.REQUEST:
        if draft.to_team_id == pool:
            return _pool_refusal(draft)
        op_type, source, dest, amount = "reallocate", pool, draft.to_team_id, draft.amount_cents
    else:
        if draft.from_team_id == draft.to_team_id:
            return Refusal(
                draft.program_id, draft.instruction, "same_team",
                "The from Team and the to Team must differ.",
            )
        op_type, source, dest, amount = (
            "reallocate", draft.from_team_id, draft.to_team_id, draft.amount_cents,
        )
    return _rules(conn, draft, op_type, source, dest, amount, now)


def _preview_offer(conn, draft: Draft, now: float | None) -> Refusal | None:
    missing = _missing(draft)
    if missing:
        return Refusal(draft.program_id, draft.instruction, "incomplete", missing)
    pool = money.pool_team_id(conn, draft.program_id)
    if draft.from_team_id == pool:
        return _pool_refusal(draft)
    return _rules(conn, draft, "reallocate", draft.from_team_id, pool, draft.amount_cents, now)


def _preview_accept(conn, draft: Draft, now: float | None) -> Refusal | None:
    if draft.offer_id is None or draft.to_team_id is None:
        return Refusal(
            draft.program_id, draft.instruction, "incomplete",
            "Accept needs an Offer and a receiving Team.",
        )
    try:
        offer = money.get_offer(conn, draft.offer_id)
    except LookupError:
        return Refusal(
            draft.program_id, draft.instruction, "unknown_offer",
            f"Offer {draft.offer_id} was not found.",
        )
    if offer.program_id != draft.program_id:
        return Refusal(
            draft.program_id, draft.instruction, "program_mismatch",
            "The Offer belongs to a different Program.",
        )
    if offer.status is not money.OfferStatus.OPEN:
        return Refusal(
            draft.program_id, draft.instruction, "offer_closed",
            f"Offer {offer.id} is {offer.status.value}.",
        )
    pool = money.pool_team_id(conn, draft.program_id)
    if draft.to_team_id == pool:
        return _pool_refusal(draft)
    if draft.to_team_id == offer.team_id:
        return Refusal(
            draft.program_id, draft.instruction, "same_team",
            "The receiving Team must differ from the Offer Team.",
        )
    return _rules(
        conn, draft, "reallocate", offer.team_id, draft.to_team_id, offer.amount_cents, now,
    )


def _preview_resolve(conn, draft: Draft) -> Refusal | None:
    if draft.operation_id is None or draft.action not in ("retry", "reject"):
        return Refusal(
            draft.program_id, draft.instruction, "incomplete",
            "A Flagged resolution needs an Operation and retry or reject.",
        )
    try:
        op = money.get_operation(conn, draft.operation_id)
    except LookupError:
        return Refusal(
            draft.program_id, draft.instruction, "unknown_operation",
            f"Operation {draft.operation_id} was not found.",
        )
    if op.program_id != draft.program_id:
        return Refusal(
            draft.program_id, draft.instruction, "program_mismatch",
            "The Operation belongs to a different Program.",
        )
    if op.status is not money.Status.FLAGGED:
        return Refusal(
            draft.program_id, draft.instruction, "not_flagged",
            f"Operation {op.id} is {op.status.value}, not flagged.",
        )
    return None


def _rules(conn, draft, op_type, source, dest, amount, now) -> Refusal | None:
    if source is None or dest is None or amount is None:
        return Refusal(draft.program_id, draft.instruction, "incomplete", _missing(draft) or "The draft is incomplete.")
    hit = money.preview_program_rules(
        conn, program_id=draft.program_id, type=op_type,
        from_team_id=source, to_team_id=dest, amount_cents=amount, now=now,
    )
    if hit is None:
        return None
    return Refusal(draft.program_id, draft.instruction, hit[0], hit[1])


def _confirm_return(conn, draft: Draft, key: str, agent_version: str, now) -> Confirmed:
    op = money.create_return(
        conn, draft.program_id, draft.from_team_id, draft.amount_cents,
        f"nl-return-{key}", agent_version, now,
    )
    return Confirmed(draft.kind, draft.program_id, f"Return queued as Operation #{op.id}", operation_id=op.id)


def _confirm_request(conn, draft: Draft, key: str, agent_version: str, now) -> Confirmed:
    op = money.create_request(
        conn, draft.program_id, draft.to_team_id, draft.amount_cents,
        f"nl-request-{key}", agent_version, now,
    )
    return Confirmed(draft.kind, draft.program_id, f"Request queued as Operation #{op.id}", operation_id=op.id)


def _confirm_reallocate(conn, draft: Draft, key: str, agent_version: str, now) -> Confirmed:
    op = money.create_operation(
        conn, program_id=draft.program_id, type="reallocate",
        from_team_id=draft.from_team_id, to_team_id=draft.to_team_id,
        amount_cents=draft.amount_cents, idempotency_key=f"nl-reallocate-{key}",
        agent_version=agent_version, now=now,
    )
    return Confirmed(
        draft.kind, draft.program_id,
        f"Admin Reallocate queued as Operation #{op.id}", operation_id=op.id,
    )


def _confirm_offer(conn, draft: Draft, key: str, agent_version: str, now) -> Confirmed:
    offer = money.post_offer(
        conn, draft.program_id, draft.from_team_id, draft.amount_cents,
        draft.settlement_ask_cents, f"nl-offer-{key}", draft.note, now,
    )
    return Confirmed(draft.kind, draft.program_id, f"Offer #{offer.id} posted", offer_id=offer.id)


def _confirm_accept(conn, draft: Draft, key: str, agent_version: str, now) -> Confirmed:
    trade = money.accept_offer(
        conn, draft.offer_id, draft.to_team_id, f"nl-trade-{key}",
        agent_version=agent_version, now=now,
    )
    return Confirmed(
        draft.kind, draft.program_id,
        f"Trade #{trade.id} queued Reallocate #{trade.operation_id}",
        operation_id=trade.operation_id, offer_id=trade.offer_id, trade_id=trade.id,
    )


def _confirm_resolve(conn, draft: Draft, key: str, agent_version: str, now) -> Confirmed:
    op = money.resolve_flagged(conn, draft.operation_id, draft.action, draft.reason, now)
    return Confirmed(
        draft.kind, draft.program_id, f"Operation #{op.id} → {op.status.value}", operation_id=op.id,
    )


_CONFIRM: dict[Kind, Callable] = {
    Kind.RETURN: _confirm_return,
    Kind.REQUEST: _confirm_request,
    Kind.REALLOCATE: _confirm_reallocate,
    Kind.OFFER: _confirm_offer,
    Kind.ACCEPT: _confirm_accept,
    Kind.RESOLVE: _confirm_resolve,
}


def _bind_return(conn, program_id, instruction, data, now) -> Draft | Refusal:
    team = _named(conn, program_id, _person(data, "team", "from_team"))
    amount = _cents(data.get("amount"))
    if isinstance(team, Refusal):
        return Refusal(program_id, instruction, team.rule, team.detail)
    if amount is None:
        return Refusal(program_id, instruction, "incomplete", "The amount must be positive.")
    pool = _pool(conn, program_id)
    draft = _draft(Kind.RETURN, program_id, instruction, team, pool, amount)
    return preview(conn, draft, now) or draft


def _bind_request(conn, program_id, instruction, data, now) -> Draft | Refusal:
    team = _named(conn, program_id, _person(data, "team", "to_team"))
    amount = _cents(data.get("amount"))
    if isinstance(team, Refusal):
        return Refusal(program_id, instruction, team.rule, team.detail)
    if amount is None:
        return Refusal(program_id, instruction, "incomplete", "The amount must be positive.")
    pool = _pool(conn, program_id)
    draft = _draft(Kind.REQUEST, program_id, instruction, pool, team, amount)
    return preview(conn, draft, now) or draft


def _bind_reallocate(conn, program_id, instruction, data, now) -> Draft | Refusal:
    source = _named(conn, program_id, _person(data, "from_team"))
    dest = _named(conn, program_id, _person(data, "to_team"))
    amount = _cents(data.get("amount"))
    for team in (source, dest):
        if isinstance(team, Refusal):
            return Refusal(program_id, instruction, team.rule, team.detail)
    if amount is None:
        return Refusal(program_id, instruction, "incomplete", "The amount must be positive.")
    draft = _draft(Kind.REALLOCATE, program_id, instruction, source, dest, amount)
    return preview(conn, draft, now) or draft


def _bind_offer(conn, program_id, instruction, data, now) -> Draft | Refusal:
    team = _named(conn, program_id, _person(data, "team", "from_team"))
    amount = _cents(data.get("amount"))
    ask = _cents(data.get("settlement_ask", data.get("ask")), allow_zero=True)
    if isinstance(team, Refusal):
        return Refusal(program_id, instruction, team.rule, team.detail)
    if amount is None:
        return Refusal(program_id, instruction, "incomplete", "The amount must be positive.")
    if ask is None:
        return Refusal(program_id, instruction, "incomplete", "The Offer needs a Settlement ask.")
    note = _clip(str(data.get("note") or "").strip(), 500)
    draft = _draft(Kind.OFFER, program_id, instruction, team, None, amount, note=note, ask=ask)
    return preview(conn, draft, now) or draft


def _bind_accept(conn, program_id, instruction, data, now) -> Draft | Refusal:
    offer_id = _pos_int(data.get("offer_id"))
    dest = _named(conn, program_id, _person(data, "to_team", "team"))
    if offer_id is None:
        return Refusal(program_id, instruction, "incomplete", "Accept needs an Offer.")
    if isinstance(dest, Refusal):
        return Refusal(program_id, instruction, dest.rule, dest.detail)
    try:
        offer = money.get_offer(conn, offer_id)
    except LookupError:
        return Refusal(program_id, instruction, "unknown_offer", f"Offer {offer_id} was not found.")
    source = _team(conn, offer.team_id)
    draft = Draft(
        kind=Kind.ACCEPT, program_id=program_id, instruction=instruction,
        from_team_id=offer.team_id, to_team_id=dest["id"],
        from_name=source["name"] if source else "", to_name=dest["name"],
        amount_cents=offer.amount_cents, settlement_ask_cents=offer.settlement_ask_cents,
        offer_id=offer.id, note=offer.note,
    )
    return preview(conn, draft, now) or draft


def _bind_resolve(conn, program_id, instruction, data, now) -> Draft | Refusal:
    operation_id = _pos_int(data.get("operation_id"))
    action = str(data.get("action") or "").strip().casefold()
    reason = _clip(str(data.get("reason") or "").strip(), 500)
    if operation_id is None or action not in ("retry", "reject"):
        return Refusal(
            program_id, instruction, "incomplete",
            "A Flagged resolution needs an Operation and retry or reject.",
        )
    try:
        op = money.get_operation(conn, operation_id)
    except LookupError:
        return Refusal(
            program_id, instruction, "unknown_operation",
            f"Operation {operation_id} was not found.",
        )
    source, dest = _team(conn, op.from_team_id), _team(conn, op.to_team_id)
    draft = Draft(
        kind=Kind.RESOLVE, program_id=program_id, instruction=instruction,
        from_team_id=op.from_team_id, to_team_id=op.to_team_id,
        from_name=source["name"] if source else "", to_name=dest["name"] if dest else "",
        amount_cents=op.amount_cents, operation_id=op.id, action=action, reason=reason,
    )
    return preview(conn, draft, now) or draft


_BIND = {
    "return": _bind_return,
    "request": _bind_request,
    "reallocate": _bind_reallocate,
    "offer": _bind_offer,
    "accept": _bind_accept,
    "resolve": _bind_resolve,
}


_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(
        r"(?i)^reject\s+(?:flagged\s+)?(?:operation\s+)?#?(\d+)(?:\s+because\s+(.+))?$"
    ), "reject"),
    (re.compile(
        r"(?i)^retry\s+(?:flagged\s+)?(?:operation\s+)?#?(\d+)(?:\s+because\s+(.+))?$"
    ), "retry"),
    (re.compile(r"(?i)^accept\s+offer\s+#?(\d+)\s+(?:as|for|by)\s+(.+)$"), "accept"),
    (re.compile(
        r"(?i)^offer\s+\$?(\d+(?:\.\d+)?)\s+from\s+(.+?)\s+for\s+\$?(\d+(?:\.\d+)?)"
        r"(?:\s+note\s+(.+))?$"
    ), "offer"),
    (re.compile(r"(?i)^reallocate\s+\$?(\d+(?:\.\d+)?)\s+from\s+(.+?)\s+to\s+(.+)$"), "reallocate"),
    (re.compile(
        r"(?i)^return\s+\$?(\d+(?:\.\d+)?)\s+from\s+(.+?)"
        r"(?:\s+to\s+the\s+(?:program\s+)?pool)?$"
    ), "return"),
    (re.compile(
        r"(?i)^request\s+\$?(\d+(?:\.\d+)?)\s+(?:for|to)\s+(.+?)"
        r"(?:\s+from\s+the\s+(?:program\s+)?pool)?$"
    ), "request"),
]


def _stub_json(instruction: str) -> str:
    text = instruction.strip().rstrip(".").strip()
    text = re.sub(r"(?i)^please\s+", "", text)
    text = re.sub(r"(?i)(\d(?:\.\d+)?)\s+dollars?\b", r"\1", text)
    text = re.sub(r"(?i)(\d(?:\.\d+)?)\s+usd\b", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, name in _PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        return json.dumps(_stub_payload(name, match))
    return json.dumps({"kind": "none", "detail": _UNREADABLE})


def _stub_payload(name: str, match: re.Match[str]) -> dict:
    if name in ("reject", "retry"):
        return {
            "kind": "resolve", "operation_id": int(match.group(1)), "action": name,
            "reason": (match.group(2) or "").strip(),
        }
    if name == "accept":
        return {"kind": "accept", "offer_id": int(match.group(1)), "to_team": match.group(2).strip()}
    if name == "offer":
        return {
            "kind": "offer", "amount": match.group(1), "team": match.group(2).strip(),
            "settlement_ask": match.group(3), "note": (match.group(4) or "").strip(),
        }
    if name == "reallocate":
        return {
            "kind": "reallocate", "amount": match.group(1),
            "from_team": match.group(2).strip(), "to_team": match.group(3).strip(),
        }
    if name == "return":
        return {"kind": "return", "amount": match.group(1), "team": match.group(2).strip()}
    return {"kind": "request", "amount": match.group(1), "team": match.group(2).strip()}


def _messages(conn, program_id: int, instruction: str) -> list[dict]:
    return [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": f"{_catalog(conn, program_id)}\n\nInstruction:\n{instruction}"},
    ]


def _catalog(conn, program_id: int) -> str:
    program = conn.execute("SELECT id, name FROM programs WHERE id = ?", (program_id,)).fetchone()
    lines = [f"Program {program['name']} id={program['id']}", "Teams:"]
    teams = conn.execute(
        "SELECT * FROM teams WHERE program_id = ? ORDER BY name", (program_id,)
    ).fetchall()
    now = time.time()
    for team in teams:
        lines.append(
            f"- {team['name']} id={team['id']}"
            f" eligible={'yes' if team['eligible'] else 'no'}"
            f" pool={'yes' if team['is_pool'] else 'no'}"
            f" ledger_usd={money.ledger_balance(conn, team['id']) / 100:.2f}"
            f" available_usd={money.available_balance(conn, team['id'], now) / 100:.2f}"
        )
    offers = conn.execute(
        "SELECT o.id, o.amount_cents, o.settlement_ask_cents, t.name AS team_name"
        " FROM offers o JOIN teams t ON t.id = o.team_id"
        " WHERE o.program_id = ? AND o.status = 'open' ORDER BY o.id",
        (program_id,),
    ).fetchall()
    lines.append("Open Offers:" if offers else "Open Offers: none")
    for offer in offers:
        lines.append(
            f"- id={offer['id']} team={offer['team_name']}"
            f" credit_usd={offer['amount_cents'] / 100:.2f}"
            f" ask_usd={offer['settlement_ask_cents'] / 100:.2f}"
        )
    flagged = conn.execute(
        "SELECT o.id, o.type, o.amount_cents, o.flag_reason, s.name AS from_name, d.name AS to_name"
        " FROM operations o"
        " JOIN teams s ON s.id = o.from_team_id JOIN teams d ON d.id = o.to_team_id"
        " WHERE o.program_id = ? AND o.status = 'flagged' ORDER BY o.id",
        (program_id,),
    ).fetchall()
    lines.append("Flagged Operations:" if flagged else "Flagged Operations: none")
    for op in flagged:
        lines.append(
            f"- id={op['id']} {op['type']} from {op['from_name']} to {op['to_name']}"
            f" amount_usd={op['amount_cents'] / 100:.2f} reason={op['flag_reason'] or ''}"
        )
    return "\n".join(lines)


def _instruction_of(messages: list[dict]) -> str:
    content = messages[-1]["content"] if messages else ""
    marker = "Instruction:\n"
    index = content.rfind(marker)
    return content[index + len(marker) :] if index >= 0 else content


def _named(conn, program_id: int, name: str):
    if not name:
        return Refusal(program_id, "", "unknown_team", "The instruction did not name a Team.")
    teams = conn.execute(
        "SELECT * FROM teams WHERE program_id = ? ORDER BY id", (program_id,)
    ).fetchall()
    folded = name.casefold()
    found = [team for team in teams if team["name"].casefold() == folded]
    if len(found) != 1:
        return Refusal(program_id, "", "unknown_team", f"No Team named {name} in this Program.")
    return found[0]


def _pool(conn, program_id: int):
    team_id = money.pool_team_id(conn, program_id)
    return _team(conn, team_id)


def _team(conn, team_id: int):
    return conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()


def _draft(kind, program_id, instruction, source, dest, amount, note="", ask=None) -> Draft:
    return Draft(
        kind=kind, program_id=program_id, instruction=instruction,
        from_team_id=source["id"], to_team_id=dest["id"] if dest is not None else None,
        from_name=source["name"], to_name=dest["name"] if dest is not None else "",
        amount_cents=amount, settlement_ask_cents=ask, note=note,
    )


def _missing(draft: Draft) -> str:
    if draft.kind is Kind.RETURN and (draft.from_team_id is None or draft.amount_cents is None):
        return "A Return needs a Team and an amount."
    if draft.kind is Kind.REQUEST and (draft.to_team_id is None or draft.amount_cents is None):
        return "A Request needs a Team and an amount."
    if draft.kind is Kind.REALLOCATE and None in (
        draft.from_team_id, draft.to_team_id, draft.amount_cents,
    ):
        return "A Reallocate needs two Teams and an amount."
    if draft.kind is Kind.OFFER and (
        draft.from_team_id is None or draft.amount_cents is None or draft.settlement_ask_cents is None
    ):
        return "An Offer needs a Team, an amount, and a Settlement ask."
    if draft.kind is Kind.ACCEPT and (draft.offer_id is None or draft.to_team_id is None):
        return "Accept needs an Offer and a receiving Team."
    if draft.kind is Kind.RESOLVE and (
        draft.operation_id is None or draft.action not in ("retry", "reject")
    ):
        return "A Flagged resolution needs an Operation and retry or reject."
    return ""


def _pool_refusal(draft: Draft) -> Refusal:
    return Refusal(
        draft.program_id, draft.instruction, "pool",
        "Pick a Team, not the Program Pool.",
    )


def _with_names(posted: dict[str, str], draft: Draft) -> Draft:
    return Draft(
        kind=draft.kind, program_id=draft.program_id, instruction=draft.instruction,
        from_team_id=draft.from_team_id, to_team_id=draft.to_team_id,
        from_name=str(posted.get("from_name") or ""),
        to_name=str(posted.get("to_name") or ""),
        amount_cents=draft.amount_cents, settlement_ask_cents=draft.settlement_ask_cents,
        offer_id=draft.offer_id, operation_id=draft.operation_id,
        action=draft.action, reason=draft.reason, note=draft.note,
    )


def _person(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _cents(value, *, allow_zero: bool = False) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        value = format(value, "f")
    elif isinstance(value, int):
        value = str(value)
    text = str(value).strip().replace(",", "").lstrip("$")
    if not text:
        return None
    try:
        cents = int((Decimal(text) * 100).to_integral_exact())
    except (InvalidOperation, ValueError):
        return None
    if cents < 0 or (cents == 0 and not allow_zero):
        return None
    return cents


def _pos_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    number = int(text)
    if number <= 0:
        return None
    return number


def _nonneg_int(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    if not text.isdigit():
        return None
    return int(text)


def _text(value: int | None) -> str:
    return "" if value is None else str(value)


def _clip(text: str, limit: int = MAX_INSTRUCTION) -> str:
    return text.strip()[:limit]


def _require_program(conn, program_id: int) -> None:
    row = conn.execute("SELECT 1 FROM programs WHERE id = ?", (program_id,)).fetchone()
    if row is None:
        raise LookupError(f"program {program_id} not found")


def _event(conn, program_id: int, step: str, detail: str, now: float | None) -> None:
    money.record_program_event(conn, program_id, step, detail, now)


def _seen(conn, program_id: int, step: str, key: str) -> bool:
    rows = conn.execute(
        "SELECT detail FROM program_audit_events WHERE program_id = ? AND step = ?",
        (program_id, step),
    )
    prefix = f"key={key};"
    return any(row["detail"].startswith(prefix) for row in rows)


def _draft_detail(draft: Draft) -> str:
    parts = [draft.kind.value]
    if draft.from_name:
        parts.append(f"from {draft.from_name}")
    if draft.to_name:
        parts.append(f"to {draft.to_name}")
    if draft.amount_cents is not None:
        parts.append(f"amount {draft.amount_cents}")
    if draft.settlement_ask_cents is not None:
        parts.append(f"ask {draft.settlement_ask_cents}")
    if draft.offer_id:
        parts.append(f"offer {draft.offer_id}")
    if draft.operation_id:
        parts.append(f"operation {draft.operation_id}")
    if draft.action:
        parts.append(draft.action)
    return " ".join(parts)


def _confirmed_detail(confirmed: Confirmed) -> str:
    parts = [confirmed.kind.value]
    if confirmed.operation_id:
        parts.append(f"operation {confirmed.operation_id}")
    if confirmed.offer_id:
        parts.append(f"offer {confirmed.offer_id}")
    if confirmed.trade_id:
        parts.append(f"trade {confirmed.trade_id}")
    return " ".join(parts)


def _trace(program_id, model, messages, output, latency_ms, outcome, result) -> None:
    session_id = f"creditpool-nl-{program_id}"
    span = {
        "name": "propose",
        "input": {"instruction": _instruction_of(messages)},
        "output": {
            "outcome": outcome,
            "rule": result.rule if isinstance(result, Refusal) else "",
            "kind": result.kind.value if isinstance(result, Draft) else "",
        },
        "error": outcome == "error",
        "started_at": time.time(),
        "ended_at": time.time(),
    }
    try:
        with prism.session(session_id):
            prism.trace_llm_call(
                session_id=session_id, agent_version="nl", model=model,
                input_messages=messages, output_message=output, latency_ms=latency_ms,
            )
            prism.submit_run(
                session_id=session_id, agent_version="nl", model=model,
                spans=[span], outcome=outcome,
            )
    except Exception:
        log.exception("PRISM trace failed")
