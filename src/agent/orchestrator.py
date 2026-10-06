"""Application service: runs one inbound message through the agent, end to end.

Per message (one DB transaction, conversation row locked):
  1. idempotency check on the channel message id (WhatsApp re-delivers webhooks);
  2. persist the inbound message (masked);
  3. extract fields/intent -> FSM decision;
  4. execute effects (quote call with resilience, handoff ticket) and feed results
     back into the FSM until no effect is left;
  5. persist snapshot + outbound messages, commit, then send the replies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime

from src.agent import responses
from src.agent.extraction import ExtractionContext, Extractor
from src.agent.fsm import ConversationFSM, Decision, OpenHandoff, Reply, RequestQuote
from src.domain.models import (
    ConversationSnapshot,
    ConversationState,
    HandoffReason,
    MessageType,
)
from src.domain.ports import (
    ConversationRecord,
    Direction,
    OutboundChannel,
    QuoteService,
    UnitOfWork,
    UnitOfWorkFactory,
)
from src.utils.crypto import contact_hash
from src.utils.logging import bind_context, bind_trace_id, current_trace_id, get_logger
from src.utils.pii import mask_text, mask_zip

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InboundMessage:
    channel: str
    channel_message_id: str
    contact_address: str
    contact_name: str | None
    message_type: MessageType
    text: str
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    conversation_id: str | None
    replies: tuple[str, ...]
    state: ConversationState | None
    duplicate: bool = False


_MAX_EFFECT_ROUNDS = 4


class Orchestrator:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        extractor: Extractor,
        fsm: ConversationFSM,
        quotes: QuoteService,
        outbound: OutboundChannel,
        today: Callable[[], date],
        contact_hash_secret: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._extractor = extractor
        self._fsm = fsm
        self._quotes = quotes
        self._outbound = outbound
        self._today = today
        self._hash_secret = contact_hash_secret

    # ------------------------------------------------------------------ inbound

    async def handle(self, msg: InboundMessage) -> TurnResult:
        trace_id = current_trace_id() or bind_trace_id()
        first_name = (msg.contact_name or "").split(" ")[0] or None
        names = tuple(n for n in (msg.contact_name, first_name) if n)
        masked = mask_text(msg.text, names)

        async with self._uow_factory() as uow:
            if await uow.message_exists(msg.channel_message_id):
                log.info("message_duplicate_ignored", channel_message_id=msg.channel_message_id)
                return TurnResult(None, (), None, duplicate=True)

            record = await uow.get_or_create_conversation(
                msg.channel,
                contact_hash(msg.contact_address, self._hash_secret),
                msg.contact_address,
            )
            bind_context(conversation_id=record.id)
            snap = record.snapshot
            if first_name and snap.profile.first_name is None:
                snap = _with_first_name(snap, first_name)
            log.info(
                "message_received",
                channel=msg.channel,
                channel_message_id=msg.channel_message_id,
                message_type=msg.message_type.value,
                body=masked,
                state=snap.state.value,
                new_conversation=record.created,
            )
            await uow.add_message(
                record.id,
                Direction.INBOUND,
                msg.message_type.value,
                masked,
                trace_id,
                snap.state,
                msg.channel_message_id,
            )

            catalog = await self._quotes.get_catalog()
            if msg.message_type is MessageType.TEXT:
                ctx = ExtractionContext(
                    state=snap.state,
                    today=self._today(),
                    expected_fields=tuple(snap.profile.missing_fields()),
                    plan_ids=tuple(p.id for p in catalog.plans)
                    if catalog
                    else ("essencial", "completo", "premium"),
                    known_names=names,
                )
                extraction = await self._extractor.extract(msg.text, ctx)
                log.info(
                    "extraction_result",
                    source=extraction.source,
                    intent=extraction.intent.value,
                    fields=[
                        f
                        for f in (
                            "age",
                            "vehicle_model",
                            "vehicle_year",
                            "zip_code",
                            "start_date",
                            "plan_id",
                        )
                        if getattr(extraction, f) is not None
                    ],
                )
                decision = self._fsm.on_message(snap, extraction, catalog, self._today())
            else:
                decision = self._fsm.on_media(snap, msg.message_type)

            outbox = _Outbox(self._outbound, record.contact_address)
            await self._run(uow, record, snap, decision, outbox, trace_id)
            await uow.commit()

        await outbox.flush()
        return TurnResult(record.id, tuple(outbox.items), record.snapshot.state)

    # ------------------------------------------------------------------ background re-quote

    async def requote(self, conversation_id: str) -> bool:
        """Retries the quote of a conversation handed off because the API was down.

        If it now succeeds and no human resolved the handoff in the meantime, the lead
        gets the quote automatically and the handoff ticket is closed.
        """
        trace_id = bind_trace_id()
        bind_context(conversation_id=conversation_id)
        async with self._uow_factory() as uow:
            record = await uow.get_conversation(conversation_id)
            if record is None or record.snapshot.state is not ConversationState.HANDOFF:
                return False
            if record.snapshot.handoff_reason is not HandoffReason.QUOTE_UNAVAILABLE:
                return False
            result = await self._quotes.quote(record.snapshot.profile)
            await uow.add_quote(record.id, result, trace_id)
            decision = self._fsm.on_quote_result(record.snapshot, result)
            if decision.snapshot.state is ConversationState.HANDOFF:
                await uow.commit()  # keep the failed attempt in the audit trail
                return False
            outbox = _Outbox(self._outbound, record.contact_address)
            await self._run(uow, record, record.snapshot, decision, outbox, trace_id)
            await uow.resolve_handoffs(record.id, "auto_requoted")
            log.info("handoff_auto_resolved", reason=HandoffReason.QUOTE_UNAVAILABLE.value)
            await uow.commit()
        await outbox.flush()
        return True

    # ------------------------------------------------------------------ internals

    async def _run(
        self,
        uow: UnitOfWork,
        record: ConversationRecord,
        before: ConversationSnapshot,
        decision: Decision,
        outbox: _Outbox,
        trace_id: str,
    ) -> None:
        for _ in range(_MAX_EFFECT_ROUNDS):
            outbox.add(self._render(decision.replies))
            next_decision: Decision | None = None
            for effect in decision.effects:
                if isinstance(effect, RequestQuote):
                    result = await self._quotes.quote(
                        decision.snapshot.profile, on_retry=_delayed_notice(outbox)
                    )
                    await uow.add_quote(record.id, result, trace_id)
                    next_decision = self._fsm.on_quote_result(decision.snapshot, result)
                elif isinstance(effect, OpenHandoff):
                    await self._open_handoff(uow, record.id, decision.snapshot, effect, trace_id)
            if next_decision is None:
                break
            decision = next_decision
        else:  # pragma: no cover - guarded by FSM design, kept as a safety net
            log.error("effect_loop_limit_reached")

        if decision.snapshot.state is not before.state:
            log.info(
                "state_transition",
                from_state=before.state.value,
                to_state=decision.snapshot.state.value,
            )
        record.snapshot = decision.snapshot
        await uow.save_conversation(record)
        for text in outbox.items:
            await uow.add_message(
                record.id,
                Direction.OUTBOUND,
                MessageType.TEXT.value,
                mask_text(text),
                trace_id,
                decision.snapshot.state,
            )

    async def _open_handoff(
        self,
        uow: UnitOfWork,
        conversation_id: str,
        snap: ConversationSnapshot,
        effect: OpenHandoff,
        trace_id: str,
    ) -> None:
        summary = _handoff_summary(snap)
        handoff_id = await uow.open_handoff(
            conversation_id, effect.reason, effect.detail, summary, trace_id
        )
        log.warning(
            "handoff_created",
            handoff_id=handoff_id,
            reason=effect.reason.value,
            detail=effect.detail,
            summary=summary,
        )

    @staticmethod
    def _render(replies: tuple[Reply, ...]) -> list[str]:
        return [responses.render(r) for r in replies]


class _Outbox:
    """Replies of one turn. Normally flushed after commit; flushed early only to keep
    message order when the lead must be told that the quote is taking longer."""

    def __init__(self, channel: OutboundChannel, address: str) -> None:
        self._channel = channel
        self._address = address
        self.items: list[str] = []
        self._sent = 0

    def add(self, texts: list[str]) -> None:
        self.items.extend(texts)

    async def flush(self) -> None:
        pending, self._sent = self.items[self._sent :], len(self.items)
        for text in pending:
            try:
                await self._channel.send(self._address, text)
            except Exception as exc:
                log.exception("outbound_send_failed", error=repr(exc))


def _delayed_notice(outbox: _Outbox) -> Callable[[int], Awaitable[None]]:
    """Retry hook: tells the lead once that the quote is taking longer than usual."""
    notified = False

    async def notify(_failed_attempt: int) -> None:
        nonlocal notified
        if not notified:
            notified = True
            outbox.add([responses.render(Reply("quote_delayed"))])
            await outbox.flush()

    return notify


def _with_first_name(snap: ConversationSnapshot, first_name: str) -> ConversationSnapshot:
    return replace(snap, profile=snap.profile.merge(first_name=first_name))


def _handoff_summary(snap: ConversationSnapshot) -> str:
    """What the seller needs to pick up without re-asking anything (CEP masked)."""
    p = snap.profile
    parts = [
        f"state={snap.state.value}",
        f"vehicle={p.vehicle_model or '?'} {p.vehicle_year or '?'}",
        f"age={p.age if p.age is not None else '?'}",
        f"cep={mask_zip(p.zip_code) if p.zip_code else '?'}",
        f"start={p.start_date.isoformat() if p.start_date else '?'}",
        f"plan={p.plan_id or '?'}",
    ]
    if snap.last_quote:
        parts.append(f"last_quote={snap.last_quote.plan_id}:{snap.last_quote.monthly_premium}")
    return "; ".join(parts)
