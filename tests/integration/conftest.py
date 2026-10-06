from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from src.infra.db.models import Base
from src.infra.db.session import create_engine, create_schema


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """TEST_DATABASE_URL if set (CI service / docker compose), else a throwaway
    testcontainers PostgreSQL, else skip."""
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer("postgres:16-alpine", driver="asyncpg")
        container.start()
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc!r}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
async def any_engine(request: pytest.FixtureRequest, sqlite_url: str) -> AsyncIterator[AsyncEngine]:
    url = sqlite_url if request.param == "sqlite" else request.getfixturevalue("postgres_url")
    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_schema(engine)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
