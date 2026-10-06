"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.middleware import TraceIdMiddleware
from src.api.routes import conversations, health, webhook
from src.bootstrap import Container, build_container
from src.config import Settings, get_settings
from src.utils.logging import configure_logging, get_logger

log = get_logger(__name__)


def create_app(container: Container | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        c = container or build_container(settings)
        app.state.container = c
        await c.quote_service.get_catalog()  # warm the cache; failure is tolerated
        if settings.requote_worker_enabled:
            c.worker.start()
        log.info("app_started", environment=settings.environment)
        try:
            yield
        finally:
            await c.worker.stop()
            if owned:
                await c.aclose()
            log.info("app_stopped")

    app = FastAPI(title="AutoQuote AI", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TraceIdMiddleware)
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(conversations.router)
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory src.api.app:app_factory`."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file)
    return create_app(settings=settings)
