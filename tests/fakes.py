"""Hand-written test doubles for the ports."""

from __future__ import annotations

import dataclasses
import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Any

from src.domain.models import LeadProfile, PlanCatalog, Quote
from src.domain.ports import QuoteAttemptInfo, QuoteResult, QuoteStatus, RetryHook
from src.llm.port import LLMUnavailableError


def make_quote(plan_id: str = "completo", premium: str = "209.90", **raw: Any) -> Quote:
    return Quote(
        plan_id=plan_id,
        plan_name=plan_id.capitalize(),
        monthly_premium=Decimal(premium),
        deductible=Decimal("3000"),
        coverages=("colisao", "roubo", "furto"),
        waiting_period_coverages=("roubo", "furto"),
        waiting_period_days=30,
        currency="BRL",
        raw={
            "plano_id": plan_id,
            "plano_nome": plan_id.capitalize(),
            "premio_mensal": float(premium),
            "franquia": 3000,
            "coberturas": ["colisao", "roubo", "furto"],
            "carencia": {"coberturas": ["roubo", "furto"], "dias": 30},
            "moeda": "BRL",
            **raw,
        },
    )


class FakeQuoteService:
    def __init__(self, catalog: PlanCatalog | None, results: list[QuoteResult] | None = None):
        self.catalog = catalog
        self.results = list(results or [])
        self.calls: list[LeadProfile] = []
        self.retry_hook_calls = 0
        self.fire_retry_hook = False

    async def get_catalog(self) -> PlanCatalog | None:
        return self.catalog

    async def quote(self, profile: LeadProfile, on_retry: RetryHook | None = None) -> QuoteResult:
        self.calls.append(profile)
        if self.fire_retry_hook and on_retry is not None:
            self.retry_hook_calls += 1
            await on_retry(1)
        if self.results:
            result = self.results.pop(0)
            return dataclasses.replace(result, request_id=uuid.uuid4().hex)
        return success(profile.plan_id or "essencial")


def success(plan_id: str = "completo", premium: str = "209.90") -> QuoteResult:
    return QuoteResult(
        QuoteStatus.SUCCESS,
        uuid.uuid4().hex,
        {},
        make_quote(plan_id, premium),
        attempts=(QuoteAttemptInfo(1, "success", 5, 200, None),),
    )


def failure(status: QuoteStatus, detail: str = "x") -> QuoteResult:
    return QuoteResult(
        status,
        uuid.uuid4().hex,
        {},
        None,
        detail,
        attempts=(QuoteAttemptInfo(1, "server_error", 5, 503, None),),
    )


class RecordingSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent: defaultdict[str, list[str]] = defaultdict(list)
        self.fail = fail

    async def send(self, contact_address: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("channel down")
        self.sent[contact_address].append(text)


class FakeLLM:
    name = "fake-llm"

    def __init__(self, response: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.response = response or {}
        self.fail = fail
        self.prompts: list[tuple[str, str]] = []

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        self.prompts.append((system, user))
        if self.fail:
            raise LLMUnavailableError("rate limited")
        return self.response
