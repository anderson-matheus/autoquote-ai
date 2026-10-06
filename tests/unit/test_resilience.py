from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.tools.resilience import CircuitBreaker, CircuitOpenError, CircuitState, RetryPolicy
from tests.conftest import FakeClock


@given(attempt=st.integers(1, 20), seed=st.integers())
def test_backoff_is_full_jitter_and_capped(attempt: int, seed: int) -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_s=0.5, max_delay_s=3.0)
    delay = policy.backoff(attempt, random.Random(seed))
    assert 0 <= delay <= min(3.0, 0.5 * 2 ** (attempt - 1))


def _breaker(clock: FakeClock, changes: list[tuple[str, str]] | None = None) -> CircuitBreaker:
    return CircuitBreaker(
        "t",
        failure_threshold=3,
        recovery_timeout_s=30,
        clock=clock,
        on_state_change=(lambda _n, a, b: changes.append((a.value, b.value)))
        if changes is not None
        else None,
    )


def test_opens_after_threshold_and_rejects(clock: FakeClock) -> None:
    changes: list[tuple[str, str]] = []
    b = _breaker(clock, changes)
    for _ in range(3):
        b.acquire()
        b.record_failure()
    assert b.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError) as exc:
        b.acquire()
    assert exc.value.retry_after_s == pytest.approx(30)
    assert changes == [("closed", "open")]


def test_success_resets_failure_count(clock: FakeClock) -> None:
    b = _breaker(clock)
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    assert b.state is CircuitState.CLOSED
    assert b.consecutive_failures == 1


def test_half_open_allows_single_probe_then_closes(clock: FakeClock) -> None:
    changes: list[tuple[str, str]] = []
    b = _breaker(clock, changes)
    for _ in range(3):
        b.record_failure()
    clock.advance(30)
    assert b.state is CircuitState.HALF_OPEN
    b.acquire()  # the probe
    with pytest.raises(CircuitOpenError):
        b.acquire()  # concurrent call while probing
    b.record_success()
    assert b.state is CircuitState.CLOSED
    assert changes == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]


def test_failed_probe_reopens(clock: FakeClock) -> None:
    b = _breaker(clock)
    for _ in range(3):
        b.record_failure()
    clock.advance(31)
    b.acquire()
    b.record_failure()
    assert b.state is CircuitState.OPEN
    clock.advance(29)
    assert b.state is CircuitState.OPEN
