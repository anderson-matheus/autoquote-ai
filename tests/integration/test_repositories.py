from __future__ import annotations

import asyncio
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.domain.models import ConversationSnapshot, ConversationState, HandoffReason, LeadProfile
from src.domain.ports import Direction
from src.infra.db.models import HandoffRow
from src.infra.db.repositories import SqlUnitOfWork
from src.infra.db.session import create_sessionmaker
from src.utils.crypto import FieldCipher
from tests.fakes import make_quote, success

CIPHER = FieldCipher.ephemeral()


def uow(engine: AsyncEngine) -> SqlUnitOfWork:
    return SqlUnitOfWork(create_sessionmaker(engine), CIPHER)


async def test_snapshot_roundtrip_with_encrypted_pii(any_engine: AsyncEngine) -> None:
    profile = LeadProfile(
        "Ana", "Toyota Corolla", 2022, 35, "01310-100", date(2026, 10, 15), "completo"
    )
    async with uow(any_engine) as u:
        record = await u.get_or_create_conversation("whatsapp", "hash1", "5511999990000")
        assert record.created
        record.snapshot = ConversationSnapshot(
            state=ConversationState.PRESENTING,
            profile=profile,
            stalled_turns=1,
            objections=1,
            last_quote=make_quote(),
            quoted_fingerprint=profile.quote_fingerprint(),
            offered_plan_id="essencial",
        )
        await u.save_conversation(record)
        await u.commit()

    async with uow(any_engine) as u:
        loaded = await u.get_or_create_conversation("whatsapp", "hash1", "ignored")
    assert not loaded.created
    assert loaded.contact_address == "5511999990000"
    assert loaded.snapshot.profile == profile
    assert loaded.snapshot.last_quote == make_quote()
    assert loaded.snapshot.quoted_fingerprint == profile.quote_fingerprint()

    async with any_engine.connect() as conn:
        raw = " ".join(
            str(v)
            for row in (await conn.execute(text("SELECT * FROM conversations"))).all()
            for v in row
        )
    assert "01310" not in raw  # CEP encrypted at rest
    assert "5511999990000" not in raw  # phone encrypted, lookup by HMAC


async def test_uncommitted_work_is_rolled_back(any_engine: AsyncEngine) -> None:
    async with uow(any_engine) as u:
        await u.get_or_create_conversation("whatsapp", "h", "a")
    async with uow(any_engine) as u:
        assert (await u.get_or_create_conversation("whatsapp", "h", "a")).created


async def test_messages_idempotency_quotes_and_handoffs(any_engine: AsyncEngine) -> None:
    async with uow(any_engine) as u:
        rec = await u.get_or_create_conversation("whatsapp", "h2", "addr")
        await u.add_message(
            rec.id, Direction.INBOUND, "text", "oi", "t1", ConversationState.NEW, "wamid.1"
        )
        await u.add_quote(rec.id, success(), "t1")
        handoff_id = await u.open_handoff(rec.id, HandoffReason.QUOTE_UNAVAILABLE, "x", "s", "t1")
        rec.snapshot = ConversationSnapshot(
            state=ConversationState.HANDOFF, handoff_reason=HandoffReason.QUOTE_UNAVAILABLE
        )
        await u.save_conversation(rec)
        await u.commit()

    async with uow(any_engine) as u:
        assert await u.message_exists("wamid.1")
        assert not await u.message_exists("wamid.2")
        assert await u.requote_candidates(max_age_s=600, limit=10) == [rec.id]
        assert await u.requote_candidates(max_age_s=-1, limit=10) == []
        await u.resolve_handoffs(rec.id, "auto_requoted")
        await u.commit()

    async with uow(any_engine) as u:
        assert await u.requote_candidates(max_age_s=600, limit=10) == []
        row = (await u.session.execute(select(HandoffRow))).scalar_one()
        assert (row.id, row.status, row.resolution) == (handoff_id, "resolved", "auto_requoted")
        assert await u.get_conversation("missing") is None


async def test_concurrent_first_messages_create_one_conversation(any_engine: AsyncEngine) -> None:
    if any_engine.dialect.name == "sqlite":
        return  # SQLite serialises writers globally; the race only exists on PostgreSQL

    async def first_message() -> str:
        async with uow(any_engine) as u:
            rec = await u.get_or_create_conversation("whatsapp", "race", "addr")
            await u.commit()
            return rec.id

    ids = await asyncio.gather(*(first_message() for _ in range(5)))
    assert len(set(ids)) == 1
