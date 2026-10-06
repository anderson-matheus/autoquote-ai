"""Composition root: the only place that knows which adapter implements which port."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.agent.extraction import Extractor, HybridExtractor, RuleBasedExtractor
from src.agent.fsm import ConversationFSM
from src.agent.handoff_policy import HandoffPolicy
from src.agent.orchestrator import Orchestrator
from src.agent.requote_worker import RequoteWorker
from src.channels.whatsapp import SimulatedWhatsAppSender, WhatsAppCloudSender
from src.config import Settings
from src.domain.ports import OutboundChannel
from src.infra.db.repositories import SqlUnitOfWork
from src.infra.db.session import create_engine, create_sessionmaker
from src.llm.openai_compat import OpenAICompatibleClient
from src.llm.port import LLMClient
from src.tools.http_client import CallBudget, ResilientHttpClient
from src.tools.quote_client import HttpQuoteService
from src.tools.resilience import CircuitBreaker, Clock, RetryPolicy, SystemClock
from src.utils.crypto import FieldCipher
from src.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    quote_service: HttpQuoteService
    orchestrator: Orchestrator
    worker: RequoteWorker
    outbound: OutboundChannel
    cipher: FieldCipher
    _clients: list[httpx.AsyncClient] = field(default_factory=list)

    def uow(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self.sessions, self.cipher)

    async def aclose(self) -> None:
        await self.worker.stop()
        for client in self._clients:
            await client.aclose()
        await self.engine.dispose()


def build_container(
    settings: Settings,
    *,
    outbound: OutboundChannel | None = None,
    clock: Clock | None = None,
    today: Callable[[], date] = date.today,
    quote_transport: httpx.AsyncBaseTransport | None = None,
    llm: LLMClient | None = None,
    rng: random.Random | None = None,
) -> Container:
    clock = clock or SystemClock()
    engine = create_engine(settings.database_url)
    sessions = create_sessionmaker(engine)

    if settings.pii_encryption_key is not None:
        cipher = FieldCipher(settings.pii_encryption_key.get_secret_value())
    else:
        if settings.environment == "production":
            raise RuntimeError("PII_ENCRYPTION_KEY is required in production")
        log.warning("pii_encryption_key_missing_using_dev_key")
        cipher = FieldCipher.from_passphrase(settings.contact_hash_secret.get_secret_value())

    quote_http = httpx.AsyncClient(transport=quote_transport)
    clients = [quote_http]

    def breaker(name: str) -> CircuitBreaker:
        return CircuitBreaker(
            name,
            failure_threshold=settings.breaker_failure_threshold,
            recovery_timeout_s=settings.breaker_recovery_timeout_s,
            clock=clock,
        )

    quote_service = HttpQuoteService(
        http=ResilientHttpClient(
            quote_http,
            RetryPolicy(
                settings.quote_max_attempts,
                settings.quote_backoff_base_s,
                settings.quote_backoff_max_s,
            ),
            CallBudget(settings.quote_attempt_timeout_s, settings.quote_total_deadline_s),
            clock,
            rng,
        ),
        base_url=settings.quote_service_url,
        quote_breaker=breaker("quote-api"),
        catalog_breaker=breaker("catalog-api"),
        clock=clock,
        catalog_ttl_s=settings.catalog_cache_ttl_s,
    )

    if llm is None and settings.llm_enabled and settings.llm_api_key is not None:
        llm_http = httpx.AsyncClient()
        clients.append(llm_http)
        llm = OpenAICompatibleClient(
            llm_http,
            settings.llm_base_url,
            settings.llm_api_key.get_secret_value(),
            settings.llm_model,
            settings.llm_timeout_s,
            CircuitBreaker("llm", failure_threshold=3, recovery_timeout_s=60.0, clock=clock),
        )
    extractor: Extractor = HybridExtractor(RuleBasedExtractor(), llm)
    log.info("extractor_configured", llm=llm.name if llm else None)

    if outbound is None:
        token = settings.whatsapp_access_token
        if token is not None and settings.whatsapp_phone_number_id:
            wa_http = httpx.AsyncClient()
            clients.append(wa_http)
            outbound = WhatsAppCloudSender(
                wa_http,
                settings.whatsapp_api_base_url,
                settings.whatsapp_phone_number_id,
                token.get_secret_value(),
            )
        else:
            outbound = SimulatedWhatsAppSender()

    def uow_factory() -> SqlUnitOfWork:
        return SqlUnitOfWork(sessions, cipher)

    orchestrator = Orchestrator(
        uow_factory=uow_factory,
        extractor=extractor,
        fsm=ConversationFSM(
            HandoffPolicy(settings.max_stalled_turns), settings.max_start_date_days_ahead
        ),
        quotes=quote_service,
        outbound=outbound,
        today=today,
        contact_hash_secret=settings.contact_hash_secret.get_secret_value(),
    )
    worker = RequoteWorker(
        orchestrator, uow_factory, settings.requote_interval_s, settings.requote_max_age_s
    )
    return Container(
        settings=settings,
        engine=engine,
        sessions=sessions,
        quote_service=quote_service,
        orchestrator=orchestrator,
        worker=worker,
        outbound=outbound,
        cipher=cipher,
        _clients=clients,
    )
