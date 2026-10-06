"""SQLAlchemy implementation of the `UnitOfWork` port + snapshot (de)serialisation."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.domain.models import (
    ConversationSnapshot,
    ConversationState,
    HandoffReason,
    LeadProfile,
)
from src.domain.ports import ConversationRecord, Direction, QuoteResult
from src.infra.db.models import ConversationRow, HandoffRow, MessageRow, QuoteRow
from src.tools.quote_client import parse_quote
from src.utils.crypto import FieldCipher


class SqlUnitOfWork:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], cipher: FieldCipher) -> None:
        self._sessions = sessions
        self._cipher = cipher
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("unit of work used outside of `async with`")
        return self._session

    async def __aenter__(self) -> SqlUnitOfWork:
        self._session = self._sessions()  # transaction auto-begins on first statement
        return self

    async def __aexit__(self, *exc: object) -> None:
        session = self.session
        try:
            if session.in_transaction():
                await session.rollback()  # anything not explicitly committed is discarded
        finally:
            await session.close()
            self._session = None

    async def commit(self) -> None:
        await self.session.commit()

    # ---------------------------------------------------------------- conversations

    async def message_exists(self, channel_message_id: str) -> bool:
        stmt = select(MessageRow.id).where(MessageRow.channel_message_id == channel_message_id)
        return (await self.session.execute(stmt)).first() is not None

    async def get_or_create_conversation(
        self, channel: str, contact_hash: str, contact_address: str
    ) -> ConversationRecord:
        stmt = (
            select(ConversationRow)
            .where(ConversationRow.contact_hash == contact_hash)
            .with_for_update()
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return self._to_record(row)
        row = ConversationRow(
            id=uuid.uuid4().hex,
            channel=channel,
            contact_hash=contact_hash,
            contact_address_enc=self._cipher.encrypt(contact_address),
            state=ConversationState.NEW.value,
            profile={},
            stalled_turns=0,
            objections=0,
            version=1,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
        except IntegrityError:
            # Concurrent first message from the same contact: the other one won the race.
            row = (await self.session.execute(stmt)).scalar_one()
            return self._to_record(row)
        record = self._to_record(row)
        record.created = True
        return record

    async def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        stmt = (
            select(ConversationRow).where(ConversationRow.id == conversation_id).with_for_update()
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return self._to_record(row) if row else None

    async def save_conversation(self, record: ConversationRecord) -> None:
        snap = record.snapshot
        await self.session.execute(
            update(ConversationRow)
            .where(ConversationRow.id == record.id)
            .values(
                state=snap.state.value,
                profile=self._profile_to_json(snap.profile),
                stalled_turns=snap.stalled_turns,
                objections=snap.objections,
                offered_plan_id=snap.offered_plan_id,
                last_quote=snap.last_quote.raw if snap.last_quote else None,
                quoted_fingerprint=snap.quoted_fingerprint,
                handoff_reason=snap.handoff_reason.value if snap.handoff_reason else None,
                version=ConversationRow.version + 1,
                updated_at=datetime.now(UTC),
            )
        )

    # ---------------------------------------------------------------- audit trail

    async def add_message(
        self,
        conversation_id: str,
        direction: Direction,
        message_type: str,
        body_masked: str,
        trace_id: str,
        state: ConversationState,
        channel_message_id: str | None = None,
    ) -> None:
        self.session.add(
            MessageRow(
                conversation_id=conversation_id,
                direction=direction.value,
                channel_message_id=channel_message_id,
                message_type=message_type,
                body_masked=body_masked,
                state=state.value,
                trace_id=trace_id,
            )
        )
        await self.session.flush()

    async def add_quote(self, conversation_id: str, result: QuoteResult, trace_id: str) -> None:
        self.session.add(
            QuoteRow(
                id=result.request_id,
                conversation_id=conversation_id,
                status=result.status.value,
                detail=result.detail,
                request_payload=result.request_payload,
                response=result.quote.raw if result.quote else None,
                monthly_premium=str(result.quote.monthly_premium) if result.quote else None,
                attempts=[
                    {
                        "attempt": a.attempt,
                        "outcome": a.outcome,
                        "status_code": a.status_code,
                        "latency_ms": a.latency_ms,
                        "error": a.error,
                    }
                    for a in result.attempts
                ],
                trace_id=trace_id,
            )
        )
        await self.session.flush()

    async def open_handoff(
        self,
        conversation_id: str,
        reason: HandoffReason,
        detail: str | None,
        summary: str,
        trace_id: str,
    ) -> str:
        handoff_id = uuid.uuid4().hex
        self.session.add(
            HandoffRow(
                id=handoff_id,
                conversation_id=conversation_id,
                reason=reason.value,
                detail=detail,
                summary=summary,
                status="open",
                trace_id=trace_id,
            )
        )
        await self.session.flush()
        return handoff_id

    async def resolve_handoffs(self, conversation_id: str, resolution: str) -> None:
        await self.session.execute(
            update(HandoffRow)
            .where(HandoffRow.conversation_id == conversation_id, HandoffRow.status == "open")
            .values(status="resolved", resolution=resolution, resolved_at=datetime.now(UTC))
        )

    async def requote_candidates(self, max_age_s: float, limit: int) -> list[str]:
        since = datetime.now(UTC) - timedelta(seconds=max_age_s)
        stmt = (
            select(HandoffRow.conversation_id)
            .join(ConversationRow, ConversationRow.id == HandoffRow.conversation_id)
            .where(
                HandoffRow.status == "open",
                HandoffRow.reason == HandoffReason.QUOTE_UNAVAILABLE.value,
                HandoffRow.created_at >= since,
                ConversationRow.state == ConversationState.HANDOFF.value,
            )
            .order_by(HandoffRow.created_at)
            .limit(limit)
        )
        return list(dict.fromkeys((await self.session.execute(stmt)).scalars()))

    # ---------------------------------------------------------------- mapping

    def _to_record(self, row: ConversationRow) -> ConversationRecord:
        return ConversationRecord(
            id=row.id,
            channel=row.channel,
            contact_address=self._cipher.decrypt(row.contact_address_enc),
            snapshot=ConversationSnapshot(
                state=ConversationState(row.state),
                profile=self._profile_from_json(row.profile or {}),
                stalled_turns=row.stalled_turns,
                objections=row.objections,
                last_quote=parse_quote(row.last_quote) if row.last_quote else None,
                quoted_fingerprint=row.quoted_fingerprint,
                offered_plan_id=row.offered_plan_id,
                handoff_reason=HandoffReason(row.handoff_reason) if row.handoff_reason else None,
            ),
        )

    def _profile_to_json(self, p: LeadProfile) -> dict[str, Any]:
        return {
            "first_name": p.first_name,
            "vehicle_model": p.vehicle_model,
            "vehicle_year": p.vehicle_year,
            "age": p.age,
            "zip_code_enc": self._cipher.encrypt(p.zip_code) if p.zip_code else None,
            "start_date": p.start_date.isoformat() if p.start_date else None,
            "plan_id": p.plan_id,
        }

    def _profile_from_json(self, data: dict[str, Any]) -> LeadProfile:
        zip_enc = data.get("zip_code_enc")
        start = data.get("start_date")
        return LeadProfile(
            first_name=data.get("first_name"),
            vehicle_model=data.get("vehicle_model"),
            vehicle_year=data.get("vehicle_year"),
            age=data.get("age"),
            zip_code=self._cipher.decrypt(zip_enc) if zip_enc else None,
            start_date=date.fromisoformat(start) if start else None,
            plan_id=data.get("plan_id"),
        )
