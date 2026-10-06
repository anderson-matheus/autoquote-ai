"""Async engine/session factory."""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.infra.db.models import Base


def create_engine(url: str) -> AsyncEngine:
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        kwargs.update(pool_size=10, max_overflow=20)
    engine = create_async_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        _fix_sqlite_transactions(engine)
    return engine


def _fix_sqlite_transactions(engine: AsyncEngine) -> None:
    """pysqlite's own transaction handling breaks SAVEPOINT/rollback semantics; let
    SQLAlchemy emit BEGIN itself (documented recipe). SQLite is only used for tests and
    the local simulation, but it must behave like PostgreSQL there."""

    @event.listens_for(engine.sync_engine, "connect")
    def _connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    """For tests and the local simulation only; real deployments run Alembic migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
