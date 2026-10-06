from __future__ import annotations

from src.agent.extraction import Intent
from src.agent.handoff_policy import HandoffDecision, HandoffPolicy
from src.domain.models import HandoffReason
from src.domain.ports import QuoteStatus
from tests.fakes import failure

policy = HandoffPolicy(max_stalled_turns=3)


def test_intents() -> None:
    assert policy.on_intent(Intent.REQUEST_HUMAN) == HandoffDecision(HandoffReason.CUSTOMER_REQUEST)
    assert policy.on_intent(Intent.OUT_OF_SCOPE) == HandoffDecision(HandoffReason.OUT_OF_SCOPE)
    assert all(
        policy.on_intent(i) is None
        for i in Intent
        if i not in (Intent.REQUEST_HUMAN, Intent.OUT_OF_SCOPE)
    )


def test_stall_threshold() -> None:
    assert policy.on_stall(2) is None
    decision = policy.on_stall(3)
    assert decision is not None
    assert decision.reason is HandoffReason.COLLECTION_STALLED


def test_objections() -> None:
    assert policy.on_objection(0, has_cheaper_plan=True) is None
    assert policy.on_objection(1, has_cheaper_plan=True) is not None
    assert policy.on_objection(0, has_cheaper_plan=False) is not None


def test_quote_failures() -> None:
    reasons = {
        s: policy.on_quote_failure(failure(s, "why")).reason
        for s in (QuoteStatus.REFUSED, QuoteStatus.UNAVAILABLE, QuoteStatus.INVALID_REQUEST)
    }
    assert reasons == {
        QuoteStatus.REFUSED: HandoffReason.UNDERWRITING_REFUSAL,
        QuoteStatus.UNAVAILABLE: HandoffReason.QUOTE_UNAVAILABLE,
        QuoteStatus.INVALID_REQUEST: HandoffReason.SYSTEM_ERROR,
    }
