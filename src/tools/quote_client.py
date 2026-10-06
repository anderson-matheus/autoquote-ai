"""Adapter for the legacy quote service (`/planos`, `/quote`).

Translates the legacy Portuguese contract into domain types and classifies every
outcome, so the agent never has to look at HTTP details.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx

from src.domain.models import (
    AgeBand,
    LeadProfile,
    Plan,
    PlanCatalog,
    ProRata,
    Quote,
    UnderwritingRules,
)
from src.domain.ports import QuoteAttemptInfo, QuoteResult, QuoteStatus, RetryHook
from src.tools.http_client import AttemptRecord, ResilientHttpClient, UpstreamUnavailableError
from src.tools.resilience import CircuitBreaker, Clock
from src.utils.logging import get_logger
from src.utils.pii import mask_zip

log = get_logger(__name__)


class HttpQuoteService:
    def __init__(
        self,
        http: ResilientHttpClient,
        base_url: str,
        quote_breaker: CircuitBreaker,
        catalog_breaker: CircuitBreaker,
        clock: Clock,
        catalog_ttl_s: float,
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._quote_breaker = quote_breaker
        self._catalog_breaker = catalog_breaker
        self._clock = clock
        self._ttl = catalog_ttl_s
        self._catalog: PlanCatalog | None = None
        self._catalog_fetched_at = float("-inf")

    @property
    def breakers(self) -> tuple[CircuitBreaker, ...]:
        return (self._quote_breaker, self._catalog_breaker)

    async def get_catalog(self) -> PlanCatalog | None:
        if self._catalog and self._clock.monotonic() - self._catalog_fetched_at < self._ttl:
            return self._catalog
        try:
            result = await self._http.request(
                "GET", f"{self._base}/planos", self._catalog_breaker, operation="catalog"
            )
            result.response.raise_for_status()
            self._catalog = parse_catalog(result.response.json())
            self._catalog_fetched_at = self._clock.monotonic()
            log.info("catalog_refreshed", plans=[p.id for p in self._catalog.plans])
        except (UpstreamUnavailableError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # Stale-while-error: plan names/coverages rarely change; prices are never cached.
            log.warning("catalog_refresh_failed", error=repr(exc), stale=self._catalog is not None)
        return self._catalog

    async def quote(self, profile: LeadProfile, on_retry: RetryHook | None = None) -> QuoteResult:
        request_id = uuid.uuid4().hex
        payload = build_quote_payload(profile)
        safe_payload = {**payload, "cep": mask_zip(payload["cep"]) if payload.get("cep") else None}
        log.info("quote_requested", quote_request_id=request_id, payload=safe_payload)
        try:
            result = await self._http.request(
                "POST",
                f"{self._base}/quote",
                self._quote_breaker,
                json=payload,
                operation="quote",
                on_retry=on_retry,
            )
        except UpstreamUnavailableError as exc:
            log.warning("quote_unavailable", quote_request_id=request_id, reason=exc.reason.value)
            return QuoteResult(
                QuoteStatus.UNAVAILABLE,
                request_id,
                safe_payload,
                detail=exc.reason.value,
                attempts=_attempts(exc.attempts),
            )

        response, attempts = result.response, _attempts(result.attempts)
        body = _json_or_empty(response)
        if response.status_code == 200:
            try:
                quote = parse_quote(body)
            except (KeyError, ValueError, TypeError) as exc:
                log.exception(
                    "quote_response_unparseable", quote_request_id=request_id, error=repr(exc)
                )
                return QuoteResult(
                    QuoteStatus.INVALID_REQUEST,
                    request_id,
                    safe_payload,
                    detail="unparseable_response",
                    attempts=attempts,
                )
            log.info(
                "quote_succeeded",
                quote_request_id=request_id,
                plan_id=quote.plan_id,
                monthly_premium=str(quote.monthly_premium),
                attempts=len(attempts),
            )
            return QuoteResult(QuoteStatus.SUCCESS, request_id, safe_payload, quote, None, attempts)
        if response.status_code == 422:
            reason = str(body.get("motivo") or body.get("detail") or "cotação recusada")
            log.info("quote_refused", quote_request_id=request_id, reason=reason)
            return QuoteResult(
                QuoteStatus.REFUSED, request_id, safe_payload, detail=reason, attempts=attempts
            )
        log.error(
            "quote_invalid_request",
            quote_request_id=request_id,
            status_code=response.status_code,
            body=body,
        )
        return QuoteResult(
            QuoteStatus.INVALID_REQUEST,
            request_id,
            safe_payload,
            detail=f"http_{response.status_code}",
            attempts=attempts,
        )


def build_quote_payload(profile: LeadProfile) -> dict[str, Any]:
    if profile.age is None or profile.vehicle_year is None:
        raise ValueError("age and vehicle_year are required to quote")
    return {
        "plano_id": profile.plan_id or "essencial",
        "idade": profile.age,
        "veiculo_ano": profile.vehicle_year,
        "cep": profile.zip_code,
        "data_inicio": profile.start_date.isoformat() if profile.start_date else None,
    }


def parse_catalog(body: dict[str, Any]) -> PlanCatalog:
    plans = tuple(
        Plan(
            id=p["id"],
            name=p["nome"],
            base_monthly=Decimal(str(p["base_mensal"])),
            deductible=Decimal(str(p["franquia"])),
            coverages=tuple(p["coberturas"]),
        )
        for p in body["planos"]
    )
    rules = body["regras"]
    driver = tuple(
        AgeBand(b["idade_min"], b["idade_max"], bool(b.get("recusar")), b.get("motivo"))
        for b in rules["faixa_etaria"]
    )
    vehicle = tuple(
        AgeBand(b["anos_min"], b["anos_max"], bool(b.get("recusar")), b.get("motivo"))
        for b in rules["idade_veiculo"]
    )
    return PlanCatalog(plans, UnderwritingRules(driver, vehicle), body.get("moeda", "BRL"))


def parse_quote(body: dict[str, Any]) -> Quote:
    pro = body.get("primeiro_pagamento_pro_rata")
    waiting = body.get("carencia") or {}
    return Quote(
        plan_id=body["plano_id"],
        plan_name=body["plano_nome"],
        monthly_premium=Decimal(str(body["premio_mensal"])),
        deductible=Decimal(str(body["franquia"])),
        coverages=tuple(body["coberturas"]),
        waiting_period_coverages=tuple(waiting.get("coberturas", ())),
        waiting_period_days=int(waiting.get("dias", 0)),
        currency=body.get("moeda", "BRL"),
        pro_rata=ProRata(
            days_in_month=int(pro["dias_no_mes"]),
            days_charged=int(pro["dias_cobrados"]),
            first_payment=Decimal(str(pro["valor_primeiro_pagamento"])),
        )
        if pro
        else None,
        raw=body,
    )


def _attempts(records: list[AttemptRecord]) -> tuple[QuoteAttemptInfo, ...]:
    return tuple(
        QuoteAttemptInfo(r.attempt, r.outcome.value, r.latency_ms, r.status_code, r.error)
        for r in records
    )


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
