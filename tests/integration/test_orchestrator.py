from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from src.agent.extraction import HybridExtractor, RuleBasedExtractor
from src.agent.fsm import ConversationFSM
from src.agent.handoff_policy import HandoffPolicy
from src.agent.orchestrator import InboundMessage, Orchestrator
from src.agent.requote_worker import RequoteWorker
from src.domain.models import ConversationState, MessageType, PlanCatalog
from src.domain.ports import QuoteStatus
from src.infra.db.models import HandoffRow, MessageRow, QuoteRow
from src.infra.db.repositories import SqlUnitOfWork
from src.infra.db.session import create_sessionmaker
from src.utils.crypto import FieldCipher
from tests.fakes import FakeQuoteService, RecordingSender, failure

TODAY = date(2026, 10, 6)
CONTACT = "5511988887777"


@pytest.fixture
def parts(
    engine: AsyncEngine, catalog: PlanCatalog
) -> tuple[Orchestrator, FakeQuoteService, RecordingSender, AsyncEngine]:
    sessions = create_sessionmaker(engine)
    cipher = FieldCipher.ephemeral()
    quotes = FakeQuoteService(catalog)
    sender = RecordingSender()
    orch = Orchestrator(
        uow_factory=lambda: SqlUnitOfWork(sessions, cipher),
        extractor=HybridExtractor(RuleBasedExtractor(), None),
        fsm=ConversationFSM(HandoffPolicy(3), 90),
        quotes=quotes,
        outbound=sender,
        today=lambda: TODAY,
        contact_hash_secret="s",
    )
    return orch, quotes, sender, engine


_ids = iter(range(1, 10_000))


def inbound(
    text: str, mtype: MessageType = MessageType.TEXT, mid: str | None = None
) -> InboundMessage:
    return InboundMessage(
        "whatsapp", mid or f"wamid.{next(_ids)}", CONTACT, "Ana Souza", mtype, text
    )


async def converse(orch: Orchestrator, *texts: str) -> list[str]:
    replies: list[str] = []
    for t in texts:
        replies += (await orch.handle(inbound(t))).replies
    return replies


FULL = ("oi", "Corolla 2022, tenho 35 anos, cep 01310-100, começar hoje", "completo")


async def test_full_turns_are_persisted_masked_and_sent(parts) -> None:  # type: ignore[no-untyped-def]
    orch, quotes, sender, engine = parts
    await converse(orch, "oi, sou a Ana Souza, cpf 389.083.863-43", *FULL[1:])
    assert len(quotes.calls) == 1
    assert "R$ 209,90/mês" in sender.sent[CONTACT][-1]
    async with create_sessionmaker(engine)() as s:
        bodies = [m.body_masked for m in (await s.execute(select(MessageRow))).scalars()]
        quote_rows = (await s.execute(select(QuoteRow))).scalars().all()
    assert "oi, sou a [NAME], cpf [CPF]" in bodies
    assert not any("389.083" in b or "01310-100" in b for b in bodies)
    assert [q.status for q in quote_rows] == ["success"]


async def test_duplicate_delivery_is_ignored(parts) -> None:  # type: ignore[no-untyped-def]
    orch, _, sender, _ = parts
    first = await orch.handle(inbound("oi", mid="wamid.dup"))
    again = await orch.handle(inbound("oi", mid="wamid.dup"))
    assert first.replies
    assert not first.duplicate
    assert again.duplicate
    assert again.replies == ()
    assert len(sender.sent[CONTACT]) == len(first.replies)


async def test_media_message(parts) -> None:  # type: ignore[no-untyped-def]
    orch, _, _, _ = parts
    result = await orch.handle(inbound("", MessageType.AUDIO))
    assert "anexos ou áudios" in result.replies[0]


async def test_slow_quote_notifies_lead_once_in_order(parts) -> None:  # type: ignore[no-untyped-def]
    orch, quotes, sender, _ = parts
    quotes.fire_retry_hook = True
    await converse(orch, *FULL)
    sent = sender.sent[CONTACT]
    calc = next(i for i, t in enumerate(sent) if "calculando" in t)
    assert "lento" in sent[calc + 1]
    assert "R$ 209,90/mês" in sent[calc + 2]
    assert sum("lento" in t for t in sent) == 1


async def test_send_failure_does_not_lose_state(engine: AsyncEngine, catalog: PlanCatalog) -> None:
    sessions = create_sessionmaker(engine)
    orch = Orchestrator(
        lambda: SqlUnitOfWork(sessions, FieldCipher.ephemeral()),
        HybridExtractor(RuleBasedExtractor(), None),
        ConversationFSM(HandoffPolicy(3), 90),
        FakeQuoteService(catalog),
        RecordingSender(fail=True),
        lambda: TODAY,
        "s",
    )
    result = await orch.handle(inbound("oi"))
    assert result.state is ConversationState.COLLECTING


async def test_outage_handoff_then_worker_recovers(parts) -> None:  # type: ignore[no-untyped-def]
    orch, quotes, sender, engine = parts
    quotes.results = [failure(QuoteStatus.UNAVAILABLE, "retries_exhausted")]
    replies = await converse(orch, *FULL)
    assert "instável" in replies[-1]
    assert not any("/mês" in r for r in replies)  # never an invented price

    sessions = create_sessionmaker(engine)
    worker = RequoteWorker(orch, orch._uow_factory, interval_s=60, max_age_s=600)
    assert await worker.run_once() == 1
    assert "voltou" in sender.sent[CONTACT][-2]
    assert "R$ 209,90/mês" in sender.sent[CONTACT][-1]
    async with sessions() as s:
        handoff = (await s.execute(select(HandoffRow))).scalar_one()
    assert (handoff.reason, handoff.status) == ("quote_unavailable", "resolved")
    assert await worker.run_once() == 0  # nothing left to do


async def test_worker_keeps_handoff_when_api_still_down(parts) -> None:  # type: ignore[no-untyped-def]
    orch, quotes, _, engine = parts
    quotes.results = [failure(QuoteStatus.UNAVAILABLE)] * 2
    await converse(orch, *FULL)
    worker = RequoteWorker(orch, orch._uow_factory, interval_s=60, max_age_s=600)
    assert await worker.run_once() == 0
    async with create_sessionmaker(engine)() as s:
        statuses = [q.status for q in (await s.execute(select(QuoteRow))).scalars()]
    assert statuses == ["unavailable", "unavailable"]


async def test_requote_ignores_other_handoffs(parts) -> None:  # type: ignore[no-untyped-def]
    orch, _, _, _ = parts
    result = await orch.handle(inbound("quero falar com um atendente"))
    assert result.conversation_id is not None
    assert await orch.requote(result.conversation_id) is False
    assert await orch.requote("does-not-exist") is False
