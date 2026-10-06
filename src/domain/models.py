"""Core domain types. Pure data: no I/O, no framework imports."""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any


class ConversationState(StrEnum):
    NEW = "new"
    COLLECTING = "collecting"
    PLAN_SELECTION = "plan_selection"
    PRESENTING = "presenting"
    HANDOFF = "handoff"
    CLOSED = "closed"


class HandoffReason(StrEnum):
    """Every reason the agent stops and calls a human. See docs/handoff-criteria.md."""

    CUSTOMER_REQUEST = "customer_request"
    PLAN_ACCEPTED = "plan_accepted"
    QUOTE_UNAVAILABLE = "quote_unavailable"
    UNDERWRITING_REFUSAL = "underwriting_refusal"
    NEGOTIATION = "negotiation"
    COLLECTION_STALLED = "collection_stalled"
    OUT_OF_SCOPE = "out_of_scope"
    SYSTEM_ERROR = "system_error"


class MessageType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    DOCUMENT = "document"
    OTHER = "other"


class LeadField(StrEnum):
    """Lead fields the agent must collect before quoting."""

    VEHICLE_YEAR = "vehicle_year"
    AGE = "age"
    ZIP_CODE = "zip_code"
    START_DATE = "start_date"


# Collection order: vehicle first (what the lead talks about), then driver, then start date.
REQUIRED_FIELDS: tuple[LeadField, ...] = (
    LeadField.VEHICLE_YEAR,
    LeadField.AGE,
    LeadField.ZIP_CODE,
    LeadField.START_DATE,
)


@dataclass(frozen=True, slots=True)
class LeadProfile:
    """Only what the quote needs (data minimisation): no CPF, e-mail or plate is kept."""

    first_name: str | None = None
    vehicle_model: str | None = None
    vehicle_year: int | None = None
    age: int | None = None
    zip_code: str | None = None
    start_date: date | None = None
    plan_id: str | None = None

    def missing_fields(self) -> list[LeadField]:
        return [f for f in REQUIRED_FIELDS if getattr(self, f.value) is None]

    def is_complete(self) -> bool:
        return not self.missing_fields()

    def merge(self, **updates: Any) -> LeadProfile:
        clean = {k: v for k, v in updates.items() if v is not None}
        return dataclasses.replace(self, **clean) if clean else self

    def quote_fingerprint(self) -> str:
        """Digest of the fields that change the price; detects when a re-quote is needed.

        A digest (not the values) so it can be persisted without storing the raw CEP."""
        key = (self.vehicle_year, self.age, self.zip_code, self.start_date, self.plan_id)
        return hashlib.sha256(repr(key).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    name: str
    base_monthly: Decimal
    deductible: Decimal
    coverages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgeBand:
    min_age: int
    max_age: int
    refuse: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class UnderwritingRules:
    """Subset of `/planos` rules used to pre-screen leads before spending a quote call."""

    driver_age_bands: tuple[AgeBand, ...]
    vehicle_age_bands: tuple[AgeBand, ...]

    def refusal_reason(self, profile: LeadProfile, today: date) -> str | None:
        if profile.age is not None:
            reason = _band_refusal(self.driver_age_bands, profile.age)
            if reason:
                return reason
        if profile.vehicle_year is not None:
            reason = _band_refusal(self.vehicle_age_bands, today.year - profile.vehicle_year)
            if reason:
                return reason
        return None


def _band_refusal(bands: tuple[AgeBand, ...], value: int) -> str | None:
    for band in bands:
        if band.min_age <= value <= band.max_age:
            return band.reason if band.refuse else None
    return "Fora das faixas aceitas pela seguradora."


@dataclass(frozen=True, slots=True)
class PlanCatalog:
    plans: tuple[Plan, ...]
    rules: UnderwritingRules
    currency: str = "BRL"

    def get(self, plan_id: str) -> Plan | None:
        return next((p for p in self.plans if p.id == plan_id), None)

    def cheaper_than(self, plan_id: str) -> Plan | None:
        current = self.get(plan_id)
        if current is None:
            return None
        cheaper = [p for p in self.plans if p.base_monthly < current.base_monthly]
        return max(cheaper, key=lambda p: p.base_monthly) if cheaper else None


@dataclass(frozen=True, slots=True)
class ProRata:
    days_in_month: int
    days_charged: int
    first_payment: Decimal


@dataclass(frozen=True, slots=True)
class Quote:
    """A price as returned by the quote service. The agent never builds one by itself."""

    plan_id: str
    plan_name: str
    monthly_premium: Decimal
    deductible: Decimal
    coverages: tuple[str, ...]
    waiting_period_coverages: tuple[str, ...]
    waiting_period_days: int
    currency: str
    pro_rata: ProRata | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """Everything the state machine needs to decide the next step of a conversation."""

    state: ConversationState = ConversationState.NEW
    profile: LeadProfile = field(default_factory=LeadProfile)
    stalled_turns: int = 0
    objections: int = 0
    last_quote: Quote | None = None
    quoted_fingerprint: str | None = None
    offered_plan_id: str | None = None
    handoff_reason: HandoffReason | None = None
