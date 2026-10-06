"""HTTP client that wraps every call with timeout, deadline, retry and circuit breaker.

Failure taxonomy (drives what is retried):
- transient: timeout, connection error, 5xx, 429  -> retried, counts against the breaker
- definitive: any other 4xx                        -> returned to the caller, not retried;
  the upstream answered correctly, so it counts as a *success* for the breaker
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from src.tools.resilience import CircuitBreaker, CircuitOpenError, Clock, RetryPolicy
from src.utils.logging import current_trace_id, get_logger

log = get_logger(__name__)


class AttemptOutcome(StrEnum):
    SUCCESS = "success"
    CLIENT_ERROR = "client_error"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    CIRCUIT_OPEN = "circuit_open"


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt: int
    outcome: AttemptOutcome
    latency_ms: int
    status_code: int | None = None
    error: str | None = None


class FailureReason(StrEnum):
    RETRIES_EXHAUSTED = "retries_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CIRCUIT_OPEN = "circuit_open"


class UpstreamUnavailableError(Exception):
    def __init__(self, reason: FailureReason, attempts: list[AttemptRecord]) -> None:
        super().__init__(f"upstream unavailable: {reason.value} after {len(attempts)} attempt(s)")
        self.reason = reason
        self.attempts = attempts


@dataclass(slots=True)
class ResilientResponse:
    response: httpx.Response
    attempts: list[AttemptRecord] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CallBudget:
    attempt_timeout_s: float
    total_deadline_s: float


_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_S = 5.0


class ResilientHttpClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        budget: CallBudget,
        clock: Clock,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client
        self._policy = policy
        self._budget = budget
        self._clock = clock
        # Non-cryptographic RNG on purpose: it only spreads retry delays (jitter).
        self._rng = rng or random.Random()  # noqa: S311  # nosec B311

    async def request(
        self,
        method: str,
        url: str,
        breaker: CircuitBreaker,
        *,
        json: Any = None,
        operation: str,
        on_retry: Callable[[int], Awaitable[None]] | None = None,
    ) -> ResilientResponse:
        attempts: list[AttemptRecord] = []
        deadline = self._clock.monotonic() + self._budget.total_deadline_s
        headers = {"X-Trace-Id": current_trace_id() or ""}

        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                breaker.acquire()
            except CircuitOpenError as exc:
                attempts.append(AttemptRecord(attempt, AttemptOutcome.CIRCUIT_OPEN, 0))
                log.warning("upstream_call_short_circuited", operation=operation, error=str(exc))
                raise UpstreamUnavailableError(FailureReason.CIRCUIT_OPEN, attempts) from exc

            remaining = deadline - self._clock.monotonic()
            timeout = min(self._budget.attempt_timeout_s, remaining)
            started = self._clock.monotonic()
            retry_after: float | None = None
            try:
                response = await self._client.request(
                    method, url, json=json, headers=headers, timeout=timeout
                )
            except httpx.TimeoutException as exc:
                record = self._record(attempt, AttemptOutcome.TIMEOUT, started, error=repr(exc))
            except httpx.TransportError as exc:
                record = self._record(
                    attempt, AttemptOutcome.CONNECTION_ERROR, started, error=repr(exc)
                )
            else:
                if response.status_code in _RETRYABLE_STATUS:
                    record = self._record(
                        attempt, AttemptOutcome.SERVER_ERROR, started, response.status_code
                    )
                    retry_after = _parse_retry_after(response)
                else:
                    outcome = (
                        AttemptOutcome.SUCCESS
                        if response.is_success
                        else AttemptOutcome.CLIENT_ERROR
                    )
                    attempts.append(self._record(attempt, outcome, started, response.status_code))
                    breaker.record_success()
                    self._log_attempt(operation, attempts[-1])
                    return ResilientResponse(response, attempts)

            attempts.append(record)
            breaker.record_failure()
            self._log_attempt(operation, record)

            if attempt == self._policy.max_attempts:
                break
            delay = self._policy.backoff(attempt, self._rng)
            if retry_after is not None:
                delay = max(delay, min(retry_after, _MAX_RETRY_AFTER_S))
            # Not worth sleeping if what is left after the sleep cannot fit a useful attempt.
            if self._clock.monotonic() + delay >= deadline - 0.25:
                log.warning("upstream_deadline_exceeded", operation=operation, attempts=attempt)
                raise UpstreamUnavailableError(FailureReason.DEADLINE_EXCEEDED, attempts)
            log.info("upstream_retry_scheduled", operation=operation, delay_s=round(delay, 3))
            if on_retry is not None:
                await on_retry(attempt)
            await self._clock.sleep(delay)

        raise UpstreamUnavailableError(FailureReason.RETRIES_EXHAUSTED, attempts)

    def _record(
        self,
        attempt: int,
        outcome: AttemptOutcome,
        started: float,
        status_code: int | None = None,
        error: str | None = None,
    ) -> AttemptRecord:
        latency_ms = int((self._clock.monotonic() - started) * 1000)
        return AttemptRecord(attempt, outcome, latency_ms, status_code, error)

    @staticmethod
    def _log_attempt(operation: str, record: AttemptRecord) -> None:
        level = "info" if record.outcome is AttemptOutcome.SUCCESS else "warning"
        getattr(log, level)(
            "upstream_attempt",
            operation=operation,
            attempt=record.attempt,
            outcome=record.outcome.value,
            status_code=record.status_code,
            latency_ms=record.latency_ms,
            error=record.error,
        )


def _parse_retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None
