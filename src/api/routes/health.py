"""Liveness / readiness."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from src.api.dependencies import ContainerDep

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(container: ContainerDep, response: Response) -> dict[str, Any]:
    """Readiness: DB reachable. Quote API state is reported but does not fail readiness,
    because the agent is designed to keep serving (with handoff) while it is down."""
    checks: dict[str, Any] = {}
    try:
        async with container.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    checks["circuits"] = {b.name: b.state.value for b in container.quote_service.breakers}
    checks["catalog_cached"] = await container.quote_service.get_catalog() is not None
    return {"status": "ok" if response.status_code != 503 else "degraded", "checks": checks}
