"""SQLAlchemy ORM models. Nothing here stores raw PII:

* contact address (phone) and CEP are Fernet-encrypted;
* lookups use an HMAC of the phone (`contact_hash`);
* message bodies are stored masked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel: Mapped[str] = mapped_column(String(32))
    contact_hash: Mapped[str] = mapped_column(String(64), unique=True)
    contact_address_enc: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), index=True)
    profile: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    stalled_turns: Mapped[int] = mapped_column(Integer, default=0)
    objections: Mapped[int] = mapped_column(Integer, default=0)
    offered_plan_id: Mapped[str | None] = mapped_column(String(32))
    last_quote: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    quoted_fingerprint: Mapped[str | None] = mapped_column(String(64))
    handoff_reason: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    channel_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    message_type: Mapped[str] = mapped_column(String(16))
    body_masked: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32))
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuoteRow(Base):
    """One logical quote request (with all of its HTTP attempts)."""

    __tablename__ = "quotes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JsonType)
    response: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    monthly_premium: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HandoffRow(Base):
    __tablename__ = "handoffs"
    __table_args__ = (Index("ix_handoffs_status_reason", "status", "reason"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    reason: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    trace_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(32))
