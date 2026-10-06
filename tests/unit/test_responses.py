from __future__ import annotations

import dataclasses
import re
from decimal import Decimal

import pytest

from src.agent import responses
from src.agent.fsm import Reply
from src.domain.models import HandoffReason, LeadField, PlanCatalog, ProRata
from tests.fakes import make_quote


def test_money_format() -> None:
    assert responses.money(Decimal("1234.5")) == "R$ 1.234,50"
    assert responses.money(Decimal("209.9")) == "R$ 209,90"


@pytest.mark.parametrize("reason", list(HandoffReason))
def test_every_handoff_reason_has_copy(reason: HandoffReason) -> None:
    assert responses.render(Reply(f"handoff_{reason.value}", {"detail": "motivo."}))


def test_no_template_can_contain_a_price_without_a_quote(catalog: PlanCatalog) -> None:
    """Guardrail: only quote-derived replies may show monthly prices."""
    replies = [Reply(k) for k in responses._STATIC] + [
        Reply("ask_fields", {"fields": list(LeadField)}),
        Reply("invalid_field", {"field": "x", "message": "y"}),
        Reply("media_not_supported", {"media": "audio"}),
        Reply("offer_cheaper", {"plan": catalog.plans[0]}),
        Reply("offer_cheaper", {"plan": None}),
        Reply("plan_menu", {"plans": catalog.plans}),
        *(Reply(f"handoff_{r.value}") for r in HandoffReason),
    ]
    for reply in replies:
        assert not re.search(r"R\$ [\d.,]+/m[eê]s", responses.render(reply)), reply.key


def test_quote_rendering_uses_api_values() -> None:
    quote = make_quote("completo", "272.87")
    text = responses.render(Reply("quote_presented", {"quote": quote}))
    assert "R$ 272,87/mês" in text
    assert "30 dias" in text


def test_pro_rata_is_explained() -> None:
    quote = dataclasses.replace(make_quote(), pro_rata=ProRata(31, 17, Decimal("115.11")))
    text = responses.render(Reply("quote_presented", {"quote": quote}))
    assert "R$ 115,11 (17 de 31 dias)" in text
    details = responses.render(Reply("quote_details", {"quote": quote}))
    assert "carência" in details


def test_field_questions_are_joined_naturally() -> None:
    one = responses.render(Reply("ask_fields", {"fields": [LeadField.AGE]}))
    two = responses.render(Reply("ask_fields", {"fields": [LeadField.AGE, LeadField.ZIP_CODE]}))
    assert "idade" in one
    assert " e o *CEP*" in two
