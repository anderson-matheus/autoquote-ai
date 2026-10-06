from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from src.domain.models import LeadProfile
from src.domain.ports import QuoteStatus
from src.tools.http_client import CallBudget, ResilientHttpClient
from src.tools.quote_client import HttpQuoteService, build_quote_payload, parse_quote
from src.tools.resilience import CircuitBreaker, RetryPolicy
from tests.conftest import FakeClock

BASE = "http://legacy.test"
PROFILE = LeadProfile(
    vehicle_year=2022,
    age=35,
    zip_code="01310-100",
    start_date=date(2026, 10, 15),
    plan_id="completo",
)


def _service(clock: FakeClock) -> HttpQuoteService:
    http = ResilientHttpClient(
        httpx.AsyncClient(),
        RetryPolicy(3, 0.0, 0.0),
        CallBudget(3.0, 12.0),
        clock,
        random.Random(0),
    )
    return HttpQuoteService(
        http,
        BASE,
        CircuitBreaker("q", 5, 30, clock),
        CircuitBreaker("c", 5, 30, clock),
        clock,
        catalog_ttl_s=60,
    )


QUOTE_BODY: dict[str, Any] = {
    "plano_id": "completo",
    "plano_nome": "Completo",
    "premio_mensal": 209.9,
    "franquia": 3000,
    "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros"],
    "multiplicadores": {},
    "carencia": {"coberturas": ["roubo", "furto"], "dias": 30},
    "moeda": "BRL",
    "primeiro_pagamento_pro_rata": {
        "dias_no_mes": 31,
        "dias_cobrados": 17,
        "valor_primeiro_pagamento": 115.11,
    },
}


def test_payload_uses_legacy_contract() -> None:
    assert build_quote_payload(PROFILE) == {
        "plano_id": "completo",
        "idade": 35,
        "veiculo_ano": 2022,
        "cep": "01310-100",
        "data_inicio": "2026-10-15",
    }
    with pytest.raises(ValueError, match="required"):
        build_quote_payload(LeadProfile(age=30))


def test_parse_quote_with_pro_rata() -> None:
    q = parse_quote(QUOTE_BODY)
    assert q.monthly_premium == Decimal("209.9")
    assert q.pro_rata is not None
    assert q.pro_rata.first_payment == Decimal("115.11")
    assert q.waiting_period_days == 30


@respx.mock
async def test_success(clock: FakeClock) -> None:
    respx.post(f"{BASE}/quote").mock(return_value=httpx.Response(200, json=QUOTE_BODY))
    result = await _service(clock).quote(PROFILE)
    assert result.status is QuoteStatus.SUCCESS
    assert result.quote is not None
    assert result.request_payload["cep"] == "01310-***"  # what gets persisted is masked


@respx.mock
async def test_refusal_is_not_retried(clock: FakeClock) -> None:
    route = respx.post(f"{BASE}/quote").mock(
        return_value=httpx.Response(422, json={"error": "cotacao_recusada", "motivo": "idade"})
    )
    result = await _service(clock).quote(PROFILE)
    assert (result.status, result.detail, route.call_count) == (QuoteStatus.REFUSED, "idade", 1)


@respx.mock
async def test_bad_request_is_a_system_error(clock: FakeClock) -> None:
    respx.post(f"{BASE}/quote").mock(return_value=httpx.Response(400, json={"error": "x"}))
    result = await _service(clock).quote(PROFILE)
    assert result.status is QuoteStatus.INVALID_REQUEST


@respx.mock
async def test_unparseable_success_is_never_shown_as_price(clock: FakeClock) -> None:
    respx.post(f"{BASE}/quote").mock(return_value=httpx.Response(200, json={"weird": True}))
    result = await _service(clock).quote(PROFILE)
    assert result.status is QuoteStatus.INVALID_REQUEST
    assert result.quote is None


@respx.mock
async def test_unavailable_after_retries(clock: FakeClock) -> None:
    respx.post(f"{BASE}/quote").mock(return_value=httpx.Response(503))
    result = await _service(clock).quote(PROFILE)
    assert result.status is QuoteStatus.UNAVAILABLE
    assert result.detail == "retries_exhausted"
    assert len(result.attempts) == 3


@respx.mock
async def test_catalog_cached_and_served_stale_on_error(
    clock: FakeClock, plans_json: dict[str, Any]
) -> None:
    route = respx.get(f"{BASE}/planos").mock(
        side_effect=[httpx.Response(200, json=plans_json)] + [httpx.Response(503)] * 3
    )
    service = _service(clock)
    first = await service.get_catalog()
    assert first is not None
    assert await service.get_catalog() is first  # within TTL: no call
    assert route.call_count == 1
    clock.advance(61)
    assert await service.get_catalog() is first  # refresh failed: stale copy
    assert route.call_count == 4


@respx.mock
async def test_catalog_none_when_never_fetched(clock: FakeClock) -> None:
    respx.get(f"{BASE}/planos").mock(return_value=httpx.Response(500))
    assert await _service(clock).get_catalog() is None
