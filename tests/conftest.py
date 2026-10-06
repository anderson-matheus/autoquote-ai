"""Shared fixtures. Tests never sleep for real and never call external services."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from src.config import Settings
from src.domain.models import PlanCatalog
from src.infra.db.session import create_engine, create_schema
from src.tools.quote_client import parse_catalog

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 6)


class FakeClock:
    """Monotonic clock that only moves when told to (or when something sleeps)."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(scope="session")
def plans_json() -> dict[str, object]:
    data: dict[str, object] = json.loads(
        (ROOT / "quote-service" / "data" / "plans.json").read_text(encoding="utf-8")
    )
    return data


@pytest.fixture(scope="session")
def catalog(plans_json: dict[str, object]) -> PlanCatalog:
    return parse_catalog(plans_json)  # type: ignore[arg-type]


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def settings(sqlite_url: str, tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        database_url=sqlite_url,
        log_file=str(tmp_path / "execution.log"),
        quote_service_url="http://quote-service.test",
        quote_backoff_base_s=0.0,
        quote_backoff_max_s=0.0,
        requote_worker_enabled=False,
        llm_api_key=None,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
async def engine(sqlite_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(sqlite_url)
    await create_schema(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
def legacy_app(monkeypatch: pytest.MonkeyPatch) -> object:
    """The real (vendored) quote-service app, in-process, with its random chaos disabled."""
    import app.main as legacy

    monkeypatch.setattr(legacy, "FAILURE_RATE", 0.0)
    monkeypatch.setattr(legacy, "SLOW_RATE", 0.0)
    return legacy


@pytest.fixture
def legacy_transport(legacy_app: object) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=legacy_app.app)  # type: ignore[attr-defined]
