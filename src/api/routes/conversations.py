"""Audit API: reconstructs what happened in a conversation (all data already masked)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from src.api.dependencies import ContainerDep, require_admin
from src.infra.db.models import ConversationRow, HandoffRow, MessageRow, QuoteRow

router = APIRouter(prefix="/conversations", tags=["audit"], dependencies=[Depends(require_admin)])


@router.get("/{conversation_id}")
async def timeline(conversation_id: str, container: ContainerDep) -> dict[str, Any]:
    async with container.sessions() as session:
        conv = await session.get(ConversationRow, conversation_id)
        if conv is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        messages = (
            await session.execute(
                select(MessageRow)
                .where(MessageRow.conversation_id == conversation_id)
                .order_by(MessageRow.id)
            )
        ).scalars()
        quotes = (
            await session.execute(
                select(QuoteRow)
                .where(QuoteRow.conversation_id == conversation_id)
                .order_by(QuoteRow.created_at)
            )
        ).scalars()
        handoffs = (
            await session.execute(
                select(HandoffRow)
                .where(HandoffRow.conversation_id == conversation_id)
                .order_by(HandoffRow.created_at)
            )
        ).scalars()
        return {
            "id": conv.id,
            "state": conv.state,
            "handoff_reason": conv.handoff_reason,
            "created_at": conv.created_at,
            "messages": [
                {
                    "direction": m.direction,
                    "type": m.message_type,
                    "body": m.body_masked,
                    "state": m.state,
                    "trace_id": m.trace_id,
                    "at": m.created_at,
                }
                for m in messages
            ],
            "quotes": [
                {
                    "id": q.id,
                    "status": q.status,
                    "detail": q.detail,
                    "monthly_premium": q.monthly_premium,
                    "request": q.request_payload,
                    "attempts": q.attempts,
                    "trace_id": q.trace_id,
                    "at": q.created_at,
                }
                for q in quotes
            ],
            "handoffs": [
                {
                    "id": h.id,
                    "reason": h.reason,
                    "detail": h.detail,
                    "summary": h.summary,
                    "status": h.status,
                    "resolution": h.resolution,
                    "trace_id": h.trace_id,
                    "at": h.created_at,
                }
                for h in handoffs
            ],
        }
