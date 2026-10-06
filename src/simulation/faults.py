"""Deterministic fault injection for demos and tests (complements the service's random chaos)."""

from __future__ import annotations

import httpx


class FaultInjectingTransport(httpx.AsyncBaseTransport):
    """Answers the first `quote_failures` POST /quote calls with HTTP 503."""

    def __init__(self, inner: httpx.AsyncBaseTransport, quote_failures: int = 0) -> None:
        self._inner = inner
        self.remaining_failures = quote_failures
        self.quote_calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/quote"):
            self.quote_calls += 1
            if self.remaining_failures > 0:
                self.remaining_failures -= 1
                return httpx.Response(
                    503, json={"error": "upstream_unavailable", "injected": True}, request=request
                )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
