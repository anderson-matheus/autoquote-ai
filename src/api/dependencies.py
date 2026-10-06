"""FastAPI dependencies."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from src.bootstrap import Container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def require_admin(
    container: ContainerDep, x_api_key: Annotated[str | None, Header()] = None
) -> None:
    expected = container.settings.admin_api_key
    if expected is None:
        if container.settings.environment == "production":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "admin API disabled")
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected.get_secret_value()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
