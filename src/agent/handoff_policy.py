"""Single source of truth for *when the agent stops and calls a human*.

Every rule is explicit, named and documented in docs/handoff-criteria.md. The guiding
principle: the agent handles the repetitive, deterministic part (collect, quote,
explain) and hands off whenever continuing would require judgement, negotiation,
authority, or would risk giving the lead a wrong answer (e.g. an invented price).

| # | Trigger                                          | Reason                 |
|---|--------------------------------------------------|------------------------|
| 1 | Lead explicitly asks for a person                | CUSTOMER_REQUEST       |
| 2 | Lead accepts a quoted plan (closing = human)     | PLAN_ACCEPTED          |
| 3 | Quote API unavailable after retries/open circuit | QUOTE_UNAVAILABLE      |
| 4 | Underwriting refusal (rules or HTTP 422)         | UNDERWRITING_REFUSAL   |
| 5 | Price objection after cheaper option was offered | NEGOTIATION            |
| 6 | N consecutive turns without progress             | COLLECTION_STALLED     |
| 7 | Claims, cancellation, complaints, other products | OUT_OF_SCOPE           |
| 8 | Our request rejected (HTTP 400) / bad response   | SYSTEM_ERROR           |
"""

from __future__ import annotations

from dataclasses import dataclass

from src.agent.extraction import Intent
from src.domain.models import HandoffReason
from src.domain.ports import QuoteResult, QuoteStatus


@dataclass(frozen=True, slots=True)
class HandoffDecision:
    reason: HandoffReason
    detail: str | None = None


class HandoffPolicy:
    def __init__(self, max_stalled_turns: int, max_objections: int = 1) -> None:
        self._max_stalled = max_stalled_turns
        self._max_objections = max_objections

    def on_intent(self, intent: Intent) -> HandoffDecision | None:
        if intent is Intent.REQUEST_HUMAN:
            return HandoffDecision(HandoffReason.CUSTOMER_REQUEST)
        if intent is Intent.OUT_OF_SCOPE:
            return HandoffDecision(HandoffReason.OUT_OF_SCOPE)
        return None

    def on_stall(self, stalled_turns: int) -> HandoffDecision | None:
        if stalled_turns >= self._max_stalled:
            return HandoffDecision(HandoffReason.COLLECTION_STALLED, f"{stalled_turns} turns")
        return None

    def on_objection(
        self, objections_so_far: int, has_cheaper_plan: bool
    ) -> HandoffDecision | None:
        """The agent may offer a cheaper plan once; discounts/negotiation are human work."""
        if objections_so_far >= self._max_objections or not has_cheaper_plan:
            return HandoffDecision(HandoffReason.NEGOTIATION)
        return None

    def on_quote_failure(self, result: QuoteResult) -> HandoffDecision:
        if result.status is QuoteStatus.REFUSED:
            return HandoffDecision(HandoffReason.UNDERWRITING_REFUSAL, result.detail)
        if result.status is QuoteStatus.UNAVAILABLE:
            return HandoffDecision(HandoffReason.QUOTE_UNAVAILABLE, result.detail)
        return HandoffDecision(HandoffReason.SYSTEM_ERROR, result.detail)
