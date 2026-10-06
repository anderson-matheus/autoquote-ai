"""Retry with exponential backoff + full jitter, and a circuit breaker.

Both take an injectable clock so tests are deterministic and never actually sleep.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from src.utils.logging import get_logger

log = get_logger(__name__)


class Clock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 0.4
    max_delay_s: float = 3.0

    def backoff(self, attempt: int, rng: random.Random) -> float:
        """Full-jitter delay before retry number `attempt` (1-based).

        Full jitter (AWS architecture blog) spreads retries of many concurrent
        conversations, avoiding synchronized retry storms against the legacy API.
        """
        ceiling = min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1)))
        return rng.uniform(0, ceiling)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    def __init__(self, name: str, retry_after_s: float) -> None:
        super().__init__(f"circuit '{name}' is open (retry in {retry_after_s:.1f}s)")
        self.name = name
        self.retry_after_s = retry_after_s


class CircuitBreaker:
    """Classic three-state breaker.

    CLOSED    -> counts consecutive failures; opens at `failure_threshold`.
    OPEN      -> rejects immediately until `recovery_timeout_s` has elapsed.
    HALF_OPEN -> lets exactly one probe through; success closes, failure re-opens.

    All methods are synchronous, so within a single event loop there is no race
    between checking and updating state.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int,
        recovery_timeout_s: float,
        clock: Clock,
        on_state_change: Callable[[str, CircuitState, CircuitState], None] | None = None,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._recovery = recovery_timeout_s
        self._clock = clock
        self._on_change = on_state_change
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        if (
            self._state is CircuitState.OPEN
            and self._clock.monotonic() - self._opened_at >= self._recovery
        ):
            self._transition(CircuitState.HALF_OPEN)
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def acquire(self) -> None:
        """Raises CircuitOpenError when the call must not be attempted."""
        state = self.state
        if state is CircuitState.OPEN:
            remaining = self._recovery - (self._clock.monotonic() - self._opened_at)
            raise CircuitOpenError(self.name, max(remaining, 0.0))
        if state is CircuitState.HALF_OPEN:
            if self._probe_in_flight:
                raise CircuitOpenError(self.name, 0.0)
            self._probe_in_flight = True

    def record_success(self) -> None:
        self._failures = 0
        self._probe_in_flight = False
        if self._state is not CircuitState.CLOSED:
            self._transition(CircuitState.CLOSED)

    def record_failure(self) -> None:
        self._failures += 1
        self._probe_in_flight = False
        if self._state is CircuitState.HALF_OPEN or self._failures >= self._threshold:
            self._opened_at = self._clock.monotonic()
            if self._state is not CircuitState.OPEN:
                self._transition(CircuitState.OPEN)

    def _transition(self, new: CircuitState) -> None:
        old, self._state = self._state, new
        log.warning(
            "circuit_state_changed",
            circuit=self.name,
            from_state=old.value,
            to_state=new.value,
            consecutive_failures=self._failures,
        )
        if self._on_change:
            self._on_change(self.name, old, new)
