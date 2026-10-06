"""Ports (hexagonal architecture): what the agent needs from the outside world."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Protocol

from src.domain.models import (
    ConversationSnapshot,
    ConversationState,
    HandoffReason,
    LeadProfile,
    PlanCatalog,
    Quote,
)


class QuoteStatus(StrEnum):
    SUCCESS = "success"
    REFUSED = "refused"  # business refusal (HTTP 422): retrying will not change it
    UNAVAILABLE = "unavailable"  # transient failures exhausted / circuit open
    INVALID_REQUEST = "invalid_request"  # HTTP 400: a bug on our side, never shown as price


@dataclass(frozen=True, slots=True)
class QuoteAttemptInfo:
    attempt: int
    outcome: str
    latency_ms: int
    status_code: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class QuoteResult:
    status: QuoteStatus
    request_id: str
    request_payload: dict[str, Any]
    quote: Quote | None = None
    detail: str | None = None
    attempts: tuple[QuoteAttemptInfo, ...] = field(default_factory=tuple)


RetryHook = Callable[[int], Awaitable[None]]
"""Called before each retry with the number of the failed attempt (UX: 'one moment...')."""


class QuoteService(Protocol):
    async def get_catalog(self) -> PlanCatalog | None:
        """Current catalog (possibly stale-cached). None only if never fetched successfully."""
        ...

    async def quote(
        self, profile: LeadProfile, on_retry: RetryHook | None = None
    ) -> QuoteResult: ...


class Today(Protocol):
    def __call__(self) -> date: ...


class OutboundChannel(Protocol):
    async def send(self, contact_address: str, text: str) -> None: ...


# ------------------------------------------------------------------ persistence


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


@dataclass(slots=True)
class ConversationRecord:
    id: str
    channel: str
    contact_address: str
    snapshot: ConversationSnapshot
    created: bool = False


class UnitOfWork(Protocol):
    """One transaction. Conversation reads take a row lock (serialises concurrent
    webhook deliveries for the same lead)."""

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(self, *exc: object) -> None: ...

    async def commit(self) -> None: ...

    async def message_exists(self, channel_message_id: str) -> bool: ...

    async def get_or_create_conversation(
        self, channel: str, contact_hash: str, contact_address: str
    ) -> ConversationRecord: ...

    async def get_conversation(self, conversation_id: str) -> ConversationRecord | None: ...

    async def save_conversation(self, record: ConversationRecord) -> None: ...

    async def add_message(
        self,
        conversation_id: str,
        direction: Direction,
        message_type: str,
        body_masked: str,
        trace_id: str,
        state: ConversationState,
        channel_message_id: str | None = None,
    ) -> None: ...

    async def add_quote(self, conversation_id: str, result: QuoteResult, trace_id: str) -> None: ...

    async def open_handoff(
        self,
        conversation_id: str,
        reason: HandoffReason,
        detail: str | None,
        summary: str,
        trace_id: str,
    ) -> str: ...

    async def resolve_handoffs(self, conversation_id: str, resolution: str) -> None: ...

    async def requote_candidates(self, max_age_s: float, limit: int) -> list[str]: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
