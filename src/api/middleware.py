"""Request-scoped trace id: taken from `X-Trace-Id` or generated, echoed in the response."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from src.utils.logging import bind_trace_id, clear_context

_VALID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        clear_context()
        incoming = request.headers.get("X-Trace-Id", "")
        trace_id = bind_trace_id(incoming.replace("-", "") if _VALID.match(incoming) else None)
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response
