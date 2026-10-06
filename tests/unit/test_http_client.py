from __future__ import annotations

import random

import httpx
import pytest
import respx

from src.tools.http_client import (
    AttemptOutcome,
    CallBudget,
    FailureReason,
    ResilientHttpClient,
    UpstreamUnavailableError,
)
from src.tools.resilience import CircuitBreaker, CircuitState, RetryPolicy
from tests.conftest import FakeClock

URL = "http://legacy.test/quote"


def _client(
    clock: FakeClock, *, attempts: int = 4, deadline: float = 12.0, base: float = 0.4
) -> ResilientHttpClient:
    return ResilientHttpClient(
        httpx.AsyncClient(),
        RetryPolicy(max_attempts=attempts, base_delay_s=base, max_delay_s=3.0),
        CallBudget(attempt_timeout_s=3.0, total_deadline_s=deadline),
        clock,
        random.Random(0),
    )


def _breaker(clock: FakeClock, threshold: int = 5) -> CircuitBreaker:
    return CircuitBreaker("quote", threshold, 30, clock)


@respx.mock
async def test_retries_transient_errors_until_success(clock: FakeClock) -> None:
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(503), httpx.ConnectTimeout("t"), httpx.Response(200, json={})]
    )
    hook_calls: list[int] = []

    async def hook(attempt: int) -> None:
        hook_calls.append(attempt)

    result = await _client(clock).request(
        "POST", URL, _breaker(clock), json={}, operation="quote", on_retry=hook
    )
    assert result.response.status_code == 200
    assert [a.outcome for a in result.attempts] == [
        AttemptOutcome.SERVER_ERROR,
        AttemptOutcome.TIMEOUT,
        AttemptOutcome.SUCCESS,
    ]
    assert route.call_count == 3
    assert hook_calls == [1, 2]
    assert len(clock.sleeps) == 2


@respx.mock
async def test_client_errors_are_not_retried_and_count_as_breaker_success(
    clock: FakeClock,
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(422, json={"motivo": "x"}))
    breaker = _breaker(clock)
    breaker.record_failure()
    result = await _client(clock).request("POST", URL, breaker, operation="quote")
    assert result.response.status_code == 422
    assert [a.outcome for a in result.attempts] == [AttemptOutcome.CLIENT_ERROR]
    assert breaker.consecutive_failures == 0


@respx.mock
async def test_exhausted_retries_raise(clock: FakeClock) -> None:
    respx.post(URL).mock(return_value=httpx.Response(502))
    with pytest.raises(UpstreamUnavailableError) as exc:
        await _client(clock, attempts=3).request("POST", URL, _breaker(clock), operation="q")
    assert exc.value.reason is FailureReason.RETRIES_EXHAUSTED
    assert len(exc.value.attempts) == 3


@respx.mock
async def test_breaker_opens_and_short_circuits(clock: FakeClock) -> None:
    route = respx.post(URL).mock(side_effect=httpx.ConnectError("down"))
    breaker = _breaker(clock, threshold=2)
    with pytest.raises(UpstreamUnavailableError) as exc:
        await _client(clock).request("POST", URL, breaker, operation="q")
    assert exc.value.reason is FailureReason.CIRCUIT_OPEN
    assert route.call_count == 2
    assert breaker.state is CircuitState.OPEN
    assert exc.value.attempts[-1].outcome is AttemptOutcome.CIRCUIT_OPEN


@respx.mock
async def test_deadline_stops_retrying(clock: FakeClock) -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        clock.advance(3.0)  # each attempt burns the whole per-attempt timeout
        raise httpx.ReadTimeout("slow")

    respx.post(URL).mock(side_effect=slow)
    with pytest.raises(UpstreamUnavailableError) as exc:
        await _client(clock, attempts=10, deadline=7.0, base=1.0).request(
            "POST", URL, _breaker(clock, 99), operation="q"
        )
    assert exc.value.reason is FailureReason.DEADLINE_EXCEEDED
    assert len(exc.value.attempts) < 10


@respx.mock
async def test_retry_after_header_is_honoured(clock: FakeClock) -> None:
    respx.post(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200)]
    )
    await _client(clock, base=0.0).request("POST", URL, _breaker(clock), operation="q")
    assert clock.sleeps == [2.0]


@respx.mock
async def test_propagates_trace_id_header(clock: FakeClock) -> None:
    from src.utils.logging import bind_trace_id, clear_context

    bind_trace_id("abc123abc123")
    route = respx.post(URL).mock(return_value=httpx.Response(200))
    try:
        await _client(clock).request("POST", URL, _breaker(clock), operation="q")
    finally:
        clear_context()
    assert route.calls[0].request.headers["X-Trace-Id"] == "abc123abc123"
