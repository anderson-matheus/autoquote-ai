"""Conversation state machine - pure decision logic, no I/O.

The orchestrator feeds events in (`on_message`, `on_media`, `on_quote_result`) and
executes the returned effects (call the quote API, open a handoff ticket). Keeping
this layer pure makes every transition unit-testable without mocks.

    NEW ──► COLLECTING ──► PLAN_SELECTION ──► (quote) ──► PRESENTING ──► HANDOFF (accepted)
               │  ▲               │                │            │
               │  └── missing ────┘                │            └──► CLOSED (declined)
               └─────────────── any state ─────────┴──► HANDOFF (see handoff_policy.py)
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.agent.extraction import Extraction, Intent
from src.agent.handoff_policy import HandoffDecision, HandoffPolicy
from src.domain import validators
from src.domain.errors import InvalidFieldError
from src.domain.models import (
    ConversationSnapshot,
    ConversationState,
    HandoffReason,
    LeadField,
    LeadProfile,
    MessageType,
    PlanCatalog,
)
from src.domain.ports import QuoteResult, QuoteStatus

S = ConversationState


# ----------------------------------------------------------------- outputs


@dataclass(frozen=True, slots=True)
class Reply:
    """A message template key + parameters, rendered to pt-BR by `responses.py`."""

    key: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RequestQuote:
    pass


@dataclass(frozen=True, slots=True)
class OpenHandoff:
    reason: HandoffReason
    detail: str | None = None


Effect = RequestQuote | OpenHandoff


@dataclass(frozen=True, slots=True)
class Decision:
    snapshot: ConversationSnapshot
    replies: tuple[Reply, ...] = ()
    effects: tuple[Effect, ...] = ()


# ----------------------------------------------------------------- machine


class ConversationFSM:
    def __init__(self, handoff_policy: HandoffPolicy, max_start_date_days_ahead: int) -> None:
        self._handoff = handoff_policy
        self._max_days = max_start_date_days_ahead

    # ---- events -------------------------------------------------------------------------

    def on_message(
        self,
        snap: ConversationSnapshot,
        ex: Extraction,
        catalog: PlanCatalog | None,
        today: date,
    ) -> Decision:
        if snap.state is S.HANDOFF:
            return Decision(snap, (Reply("handoff_ack"),))

        replies: list[Reply] = []
        if snap.state is S.NEW:
            replies.append(Reply("greeting"))
        elif snap.state is S.CLOSED:
            replies.append(Reply("welcome_back"))
            snap = dataclasses.replace(snap, state=S.COLLECTING, stalled_turns=0)

        early = self._handoff.on_intent(ex.intent)
        if early:
            return self._handoff_decision(snap, early, replies)

        profile, errors = self._apply_fields(snap.profile, ex, catalog, today)
        progressed = profile != snap.profile
        snap = dataclasses.replace(snap, profile=profile)
        replies.extend(
            Reply("invalid_field", {"field": e.field, "message": e.message}) for e in errors
        )

        refusal = catalog.rules.refusal_reason(profile, today) if catalog else None
        if refusal:
            return self._handoff_decision(
                snap, HandoffDecision(HandoffReason.UNDERWRITING_REFUSAL, refusal), replies
            )

        if snap.state is S.PRESENTING:
            return self._presenting(snap, ex, catalog, progressed, replies)
        return self._collecting(snap, ex, catalog, progressed, replies)

    def on_media(self, snap: ConversationSnapshot, media: MessageType) -> Decision:
        if snap.state is S.HANDOFF:
            return Decision(snap, (Reply("handoff_ack"),))
        replies = [Reply("media_not_supported", {"media": media.value})]
        missing = snap.profile.missing_fields()
        if snap.state in (S.NEW, S.COLLECTING) and missing:
            replies.append(Reply("ask_fields", {"fields": missing}))
        return Decision(snap, tuple(replies))

    def on_quote_result(self, snap: ConversationSnapshot, result: QuoteResult) -> Decision:
        recovered = snap.state is S.HANDOFF
        if result.status is QuoteStatus.SUCCESS and result.quote is not None:
            new = dataclasses.replace(
                snap,
                state=S.PRESENTING,
                last_quote=result.quote,
                quoted_fingerprint=snap.profile.quote_fingerprint(),
                stalled_turns=0,
                handoff_reason=None,
            )
            replies = [Reply("quote_recovered")] if recovered else []
            replies.append(Reply("quote_presented", {"quote": result.quote}))
            return Decision(new, tuple(replies))
        if recovered:
            return Decision(snap)  # background retry failed again: stay with the human
        decision = self._handoff.on_quote_failure(result)
        return self._handoff_decision(snap, decision, [])

    # ---- state handlers -----------------------------------------------------------------

    def _collecting(
        self,
        snap: ConversationSnapshot,
        ex: Extraction,
        catalog: PlanCatalog | None,
        progressed: bool,
        replies: list[Reply],
    ) -> Decision:
        if ex.intent is Intent.DECLINE:
            return _close(snap, replies)

        missing = snap.profile.missing_fields()
        if missing:
            return self._ask_or_stall(snap, S.COLLECTING, progressed, replies, missing)

        if snap.profile.plan_id:
            snap = dataclasses.replace(snap, state=S.PLAN_SELECTION, stalled_turns=0)
            return Decision(snap, (*replies, Reply("quoting")), (RequestQuote(),))

        if catalog is None:
            return self._handoff_decision(
                snap,
                HandoffDecision(HandoffReason.QUOTE_UNAVAILABLE, "catalog_unavailable"),
                replies,
            )
        entering = snap.state is not S.PLAN_SELECTION
        if not entering and not progressed:
            stalled = snap.stalled_turns + 1
            stall = self._handoff.on_stall(stalled)
            if stall:
                return self._handoff_decision(snap, stall, replies)
            snap = dataclasses.replace(snap, stalled_turns=stalled)
        elif entering:
            snap = dataclasses.replace(snap, stalled_turns=0)
        snap = dataclasses.replace(snap, state=S.PLAN_SELECTION)
        return Decision(snap, (*replies, Reply("plan_menu", {"plans": catalog.plans})))

    def _presenting(
        self,
        snap: ConversationSnapshot,
        ex: Extraction,
        catalog: PlanCatalog | None,
        progressed: bool,
        replies: list[Reply],
    ) -> Decision:
        offered, snap = snap.offered_plan_id, dataclasses.replace(snap, offered_plan_id=None)
        if offered and ex.intent is Intent.ACCEPT and not ex.plan_id:
            # "pode ser" right after we suggested a cheaper plan means "quote that one".
            snap = dataclasses.replace(snap, profile=snap.profile.merge(plan_id=offered))
            progressed = True
        if progressed and snap.profile.quote_fingerprint() != snap.quoted_fingerprint:
            return Decision(snap, (*replies, Reply("requoting")), (RequestQuote(),))

        if ex.intent is Intent.ACCEPT and snap.last_quote:
            return self._handoff_decision(
                snap,
                HandoffDecision(HandoffReason.PLAN_ACCEPTED, snap.last_quote.plan_id),
                replies,
            )
        if ex.intent is Intent.DECLINE:
            return _close(snap, replies)
        if ex.intent is Intent.OBJECTION:
            cheaper = (
                catalog.cheaper_than(snap.last_quote.plan_id)
                if catalog and snap.last_quote
                else None
            )
            negotiation = self._handoff.on_objection(snap.objections, cheaper is not None)
            if negotiation:
                return self._handoff_decision(snap, negotiation, replies)
            snap = dataclasses.replace(
                snap,
                objections=snap.objections + 1,
                offered_plan_id=cheaper.id if cheaper else None,
            )
            return Decision(snap, (*replies, Reply("offer_cheaper", {"plan": cheaper})))
        if ex.intent is Intent.ASK_DETAILS and snap.last_quote:
            return Decision(snap, (*replies, Reply("quote_details", {"quote": snap.last_quote})))

        stalled = snap.stalled_turns + 1
        stall = self._handoff.on_stall(stalled)
        if stall:
            return self._handoff_decision(snap, stall, replies)
        snap = dataclasses.replace(snap, stalled_turns=stalled)
        return Decision(snap, (*replies, Reply("next_steps")))

    # ---- helpers ------------------------------------------------------------------------

    def _ask_or_stall(
        self,
        snap: ConversationSnapshot,
        state: ConversationState,
        progressed: bool,
        replies: list[Reply],
        missing: list[LeadField],
    ) -> Decision:
        if progressed or snap.state is S.NEW:
            stalled = 0
        else:
            stalled = snap.stalled_turns + 1
            stall = self._handoff.on_stall(stalled)
            if stall:
                return self._handoff_decision(snap, stall, replies)
        snap = dataclasses.replace(snap, state=state, stalled_turns=stalled)
        return Decision(snap, (*replies, Reply("ask_fields", {"fields": missing})))

    def _handoff_decision(
        self, snap: ConversationSnapshot, decision: HandoffDecision, replies: list[Reply]
    ) -> Decision:
        snap = dataclasses.replace(snap, state=S.HANDOFF, handoff_reason=decision.reason)
        reply = Reply(f"handoff_{decision.reason.value}", {"detail": decision.detail})
        return Decision(snap, (*replies, reply), (OpenHandoff(decision.reason, decision.detail),))

    def _apply_fields(
        self,
        profile: LeadProfile,
        ex: Extraction,
        catalog: PlanCatalog | None,
        today: date,
    ) -> tuple[LeadProfile, list[InvalidFieldError]]:
        updates: dict[str, Any] = {}
        errors: list[InvalidFieldError] = []

        def attempt(name: str, fn: Any, *args: Any) -> None:
            try:
                updates[name] = fn(*args)
            except InvalidFieldError as exc:
                errors.append(exc)

        if ex.age is not None:
            attempt("age", validators.validate_age, ex.age)
        if ex.vehicle_year is not None:
            attempt("vehicle_year", validators.validate_vehicle_year, ex.vehicle_year, today)
        if ex.zip_code is not None:
            attempt("zip_code", validators.normalize_zip_code, ex.zip_code)
        if ex.start_date is not None:
            attempt(
                "start_date", validators.validate_start_date, ex.start_date, today, self._max_days
            )
        if ex.vehicle_model:
            updates["vehicle_model"] = ex.vehicle_model
        if ex.plan_id and (catalog is None or catalog.get(ex.plan_id)):
            updates["plan_id"] = ex.plan_id
        return profile.merge(**updates), errors


def _close(snap: ConversationSnapshot, replies: list[Reply]) -> Decision:
    return Decision(dataclasses.replace(snap, state=S.CLOSED), (*replies, Reply("farewell")))
