from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from src.agent.extraction import Extraction, Intent
from src.agent.fsm import ConversationFSM, Decision, OpenHandoff, RequestQuote
from src.agent.handoff_policy import HandoffPolicy
from src.domain.models import (
    ConversationSnapshot,
    ConversationState,
    HandoffReason,
    LeadField,
    LeadProfile,
    MessageType,
    PlanCatalog,
)
from src.domain.ports import QuoteStatus
from tests.fakes import failure, make_quote, success

S = ConversationState
TODAY = date(2026, 10, 6)
COMPLETE = LeadProfile(
    vehicle_year=2022, age=35, zip_code="01310-100", start_date=TODAY, vehicle_model="X"
)
fsm = ConversationFSM(HandoffPolicy(max_stalled_turns=3), max_start_date_days_ahead=90)


def msg(snap: ConversationSnapshot, catalog: PlanCatalog | None, **kw: object) -> Decision:
    return fsm.on_message(snap, Extraction(**kw), catalog, TODAY)  # type: ignore[arg-type]


def keys(d: Decision) -> list[str]:
    return [r.key for r in d.replies]


def presenting(plan: str = "premium", objections: int = 0) -> ConversationSnapshot:
    profile = dataclasses.replace(COMPLETE, plan_id=plan)
    return ConversationSnapshot(
        state=S.PRESENTING,
        profile=profile,
        last_quote=make_quote(plan),
        quoted_fingerprint=profile.quote_fingerprint(),
        objections=objections,
    )


def test_first_message_greets_and_asks_everything(catalog: PlanCatalog) -> None:
    d = msg(ConversationSnapshot(), catalog, intent=Intent.GREETING)
    assert d.snapshot.state is S.COLLECTING
    assert keys(d) == ["greeting", "ask_fields"]
    assert d.replies[1].params["fields"] == list(LeadField)


def test_collects_only_missing_fields(catalog: PlanCatalog) -> None:
    snap = ConversationSnapshot(state=S.COLLECTING)
    d = msg(snap, catalog, intent=Intent.PROVIDE_INFO, vehicle_year=2020, age=40)
    assert d.replies[-1].params["fields"] == [LeadField.ZIP_CODE, LeadField.START_DATE]
    assert d.snapshot.stalled_turns == 0


def test_invalid_values_are_explained_and_not_stored(catalog: PlanCatalog) -> None:
    d = msg(ConversationSnapshot(state=S.COLLECTING), catalog, zip_code="0131", age=5)
    assert keys(d) == ["invalid_field", "invalid_field", "ask_fields"]
    assert d.snapshot.profile.zip_code is None
    assert d.snapshot.profile.age is None


def test_complete_profile_shows_plan_menu(catalog: PlanCatalog) -> None:
    snap = ConversationSnapshot(
        state=S.COLLECTING, profile=dataclasses.replace(COMPLETE, start_date=None)
    )
    d = msg(snap, catalog, start_date=TODAY)
    assert d.snapshot.state is S.PLAN_SELECTION
    assert keys(d) == ["plan_menu"]


def test_plan_choice_requests_quote(catalog: PlanCatalog) -> None:
    snap = ConversationSnapshot(state=S.PLAN_SELECTION, profile=COMPLETE)
    d = msg(snap, catalog, plan_id="completo")
    assert d.effects == (RequestQuote(),)
    assert keys(d) == ["quoting"]


def test_unknown_plan_is_ignored(catalog: PlanCatalog) -> None:
    snap = ConversationSnapshot(state=S.PLAN_SELECTION, profile=COMPLETE)
    d = msg(snap, catalog, plan_id="gold")
    assert d.effects == ()
    assert d.snapshot.stalled_turns == 1


def test_no_catalog_hands_off_instead_of_guessing_plans() -> None:
    d = msg(ConversationSnapshot(state=S.COLLECTING, profile=COMPLETE), None)
    assert d.snapshot.state is S.HANDOFF
    assert d.effects == (OpenHandoff(HandoffReason.QUOTE_UNAVAILABLE, "catalog_unavailable"),)


def test_stall_limit_hands_off(catalog: PlanCatalog) -> None:
    snap = ConversationSnapshot(state=S.COLLECTING)
    for expected in (1, 2):
        d = msg(snap, catalog)
        snap = d.snapshot
        assert snap.stalled_turns == expected
    d = msg(snap, catalog)
    assert d.snapshot.state is S.HANDOFF
    assert d.snapshot.handoff_reason is HandoffReason.COLLECTION_STALLED


@pytest.mark.parametrize(
    ("intent", "reason"),
    [
        (Intent.REQUEST_HUMAN, HandoffReason.CUSTOMER_REQUEST),
        (Intent.OUT_OF_SCOPE, HandoffReason.OUT_OF_SCOPE),
    ],
)
@pytest.mark.parametrize("state", [S.NEW, S.COLLECTING, S.PLAN_SELECTION, S.PRESENTING])
def test_safety_intents_hand_off_from_any_state(
    catalog: PlanCatalog, intent: Intent, reason: HandoffReason, state: S
) -> None:
    d = msg(ConversationSnapshot(state=state), catalog, intent=intent)
    assert d.snapshot.state is S.HANDOFF
    assert d.effects == (OpenHandoff(reason, None),)


def test_underwriting_prescreen_refuses_before_calling_api(catalog: PlanCatalog) -> None:
    d = msg(ConversationSnapshot(state=S.COLLECTING), catalog, vehicle_year=2001)
    assert d.snapshot.handoff_reason is HandoffReason.UNDERWRITING_REFUSAL
    assert "20 anos" in str(d.effects[0].detail)  # type: ignore[union-attr]
    old_driver = msg(ConversationSnapshot(state=S.COLLECTING), catalog, age=80)
    assert old_driver.snapshot.handoff_reason is HandoffReason.UNDERWRITING_REFUSAL


def test_decline_while_collecting_closes(catalog: PlanCatalog) -> None:
    d = msg(ConversationSnapshot(state=S.COLLECTING), catalog, intent=Intent.DECLINE)
    assert (d.snapshot.state, keys(d)) == (S.CLOSED, ["farewell"])


def test_closed_conversation_reopens(catalog: PlanCatalog) -> None:
    d = msg(ConversationSnapshot(state=S.CLOSED, profile=COMPLETE), catalog)
    assert keys(d) == ["welcome_back", "plan_menu"]


def test_handoff_state_only_acknowledges(catalog: PlanCatalog) -> None:
    snap = ConversationSnapshot(state=S.HANDOFF)
    assert keys(msg(snap, catalog, age=30)) == ["handoff_ack"]
    assert keys(fsm.on_media(snap, MessageType.AUDIO)) == ["handoff_ack"]


def test_media_is_politely_rejected(catalog: PlanCatalog) -> None:
    d = fsm.on_media(ConversationSnapshot(state=S.COLLECTING), MessageType.DOCUMENT)
    assert keys(d) == ["media_not_supported", "ask_fields"]
    assert d.snapshot.stalled_turns == 0


# ------------------------------------------------------------------ quote results


def test_quote_success_presents() -> None:
    snap = ConversationSnapshot(state=S.PLAN_SELECTION, profile=COMPLETE)
    d = fsm.on_quote_result(snap, success())
    assert d.snapshot.state is S.PRESENTING
    assert d.snapshot.quoted_fingerprint == COMPLETE.quote_fingerprint()
    assert keys(d) == ["quote_presented"]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (QuoteStatus.UNAVAILABLE, HandoffReason.QUOTE_UNAVAILABLE),
        (QuoteStatus.REFUSED, HandoffReason.UNDERWRITING_REFUSAL),
        (QuoteStatus.INVALID_REQUEST, HandoffReason.SYSTEM_ERROR),
    ],
)
def test_quote_failures_hand_off_without_price(status: QuoteStatus, reason: HandoffReason) -> None:
    d = fsm.on_quote_result(ConversationSnapshot(state=S.PLAN_SELECTION), failure(status))
    assert d.snapshot.state is S.HANDOFF
    assert d.snapshot.last_quote is None
    assert keys(d) == [f"handoff_{reason.value}"]


def test_recovered_quote_after_handoff() -> None:
    snap = ConversationSnapshot(
        state=S.HANDOFF, profile=COMPLETE, handoff_reason=HandoffReason.QUOTE_UNAVAILABLE
    )
    d = fsm.on_quote_result(snap, success())
    assert keys(d) == ["quote_recovered", "quote_presented"]
    assert d.snapshot.handoff_reason is None
    still_down = fsm.on_quote_result(snap, failure(QuoteStatus.UNAVAILABLE))
    assert (still_down.snapshot, still_down.replies, still_down.effects) == (snap, (), ())


# ------------------------------------------------------------------ presenting


def test_accept_hands_off_for_closing(catalog: PlanCatalog) -> None:
    d = msg(presenting(), catalog, intent=Intent.ACCEPT)
    assert d.effects == (OpenHandoff(HandoffReason.PLAN_ACCEPTED, "premium"),)


def test_changed_data_triggers_requote(catalog: PlanCatalog) -> None:
    d = msg(presenting(), catalog, plan_id="essencial")
    assert d.effects == (RequestQuote(),)
    assert keys(d) == ["requoting"]
    same = msg(presenting(), catalog, plan_id="premium", intent=Intent.ASK_DETAILS)
    assert keys(same) == ["quote_details"]


def test_first_objection_offers_cheaper_then_affirmative_requotes(catalog: PlanCatalog) -> None:
    d = msg(presenting("premium"), catalog, intent=Intent.OBJECTION)
    assert keys(d) == ["offer_cheaper"]
    assert d.snapshot.offered_plan_id == "completo"
    d2 = msg(d.snapshot, catalog, intent=Intent.ACCEPT)
    assert d2.effects == (RequestQuote(),)
    assert d2.snapshot.profile.plan_id == "completo"
    assert d2.snapshot.offered_plan_id is None


def test_second_objection_or_no_cheaper_plan_goes_to_negotiation(catalog: PlanCatalog) -> None:
    again = msg(presenting("completo", objections=1), catalog, intent=Intent.OBJECTION)
    assert again.snapshot.handoff_reason is HandoffReason.NEGOTIATION
    cheapest = msg(presenting("essencial"), catalog, intent=Intent.OBJECTION)
    assert cheapest.snapshot.handoff_reason is HandoffReason.NEGOTIATION


def test_presenting_decline_and_unclear(catalog: PlanCatalog) -> None:
    assert msg(presenting(), catalog, intent=Intent.DECLINE).snapshot.state is S.CLOSED
    unclear = msg(presenting(), catalog)
    assert keys(unclear) == ["next_steps"]
    assert unclear.snapshot.stalled_turns == 1
