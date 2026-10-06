"""Consumer-driven contract tests: the vendored legacy service, exercised through
our own parsers, so any drift in its JSON breaks the build (not production)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from src.tools.quote_client import parse_catalog, parse_quote


@pytest.fixture
async def api(legacy_transport: httpx.ASGITransport) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=legacy_transport, base_url="http://legacy") as c:
        yield c


async def quote(api: httpx.AsyncClient, **payload: Any) -> httpx.Response:
    body = {"plano_id": "completo", "idade": 35, "veiculo_ano": 2022, **payload}
    return await api.post("/quote", json=body)


async def test_health(api: httpx.AsyncClient) -> None:
    assert (await api.get("/health")).json() == {"status": "ok"}


async def test_catalog_parses(api: httpx.AsyncClient) -> None:
    catalog = parse_catalog((await api.get("/planos")).json())
    assert [p.id for p in catalog.plans] == ["essencial", "completo", "premium"]
    assert catalog.cheaper_than("premium").id == "completo"  # type: ignore[union-attr]
    assert catalog.cheaper_than("essencial") is None


async def test_base_quote_parses(api: httpx.AsyncClient) -> None:
    q = parse_quote((await quote(api, cep="01310-100")).json())
    assert q.monthly_premium == Decimal("209.9")
    assert q.waiting_period_coverages == ("roubo", "furto")
    assert q.pro_rata is None


@pytest.mark.parametrize(
    ("payload", "premium"),
    [
        ({"idade": 20}, Decimal("335.84")),  # 18-24 -> x1.60
        ({"cep": "08010-000"}, Decimal("272.87")),  # high-risk region -> x1.30
        ({"veiculo_ano": date.today().year - 8}, Decimal("241.38")),  # 6-10y -> x1.15 (float)
    ],
)
async def test_multipliers(
    api: httpx.AsyncClient, payload: dict[str, Any], premium: Decimal
) -> None:
    assert parse_quote((await quote(api, **payload)).json()).monthly_premium == premium


async def test_pro_rata_mid_month(api: httpx.AsyncClient) -> None:
    start = (date.today().replace(day=1) + timedelta(days=40)).replace(day=15)
    q = parse_quote((await quote(api, data_inicio=start.isoformat())).json())
    assert q.pro_rata is not None
    assert q.pro_rata.first_payment < q.monthly_premium


@pytest.mark.parametrize(
    "payload", [{"idade": 80}, {"veiculo_ano": date.today().year - 25}, {"plano_id": "gold"}]
)
async def test_refusals_are_422(api: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    r = await quote(api, **payload)
    assert r.status_code == 422
    assert r.json()["error"] == "cotacao_recusada"


async def test_chaos_mode_returns_5xx(
    api: httpx.AsyncClient, legacy_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(legacy_app, "FAILURE_RATE", 1.0)
    assert (await quote(api)).status_code in (500, 502, 503)
