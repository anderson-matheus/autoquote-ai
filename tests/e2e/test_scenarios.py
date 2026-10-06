"""Every scripted scenario, end to end: real agent stack + real legacy app (in-process)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx
import pytest

from src.config import Settings
from src.simulation.runner import SimulationRunner, load_scenarios, render_transcript
from src.utils.logging import configure_logging

pytestmark = pytest.mark.e2e
SCENARIOS = load_scenarios()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario(
    scenario: object, settings: Settings, legacy_transport: httpx.ASGITransport
) -> None:
    runner = SimulationRunner(settings, legacy_transport, create_tables=True, run_tag="")
    result = await runner.run(scenario)  # type: ignore[arg-type]
    assert result.passed, render_transcript([result])


async def test_no_pii_reaches_the_execution_log(
    settings: Settings, legacy_transport: httpx.ASGITransport, tmp_path: Path
) -> None:
    log_file = tmp_path / "execution.log"
    configure_logging("DEBUG", str(log_file), stream=False)
    runner = SimulationRunner(settings, legacy_transport, create_tables=True, run_tag="")
    for scenario in SCENARIOS:
        await runner.run(scenario)
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "quote_succeeded" in content
    leaks = re.findall(
        r"389\.083\.863-43|ursula\.souza@|97224-2584|01310-100|08010-000|30140-071|"
        r"Ursula|Felipe Costa|5511900000",
        content,
    )
    assert leaks == []


async def test_total_outage_never_invents_a_price(
    settings: Settings,
    legacy_app: object,
    legacy_transport: httpx.ASGITransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_app, "FAILURE_RATE", 1.0)
    scenario = next(s for s in SCENARIOS if s.name == "happy_path")
    runner = SimulationRunner(settings, legacy_transport, create_tables=True, run_tag="")
    result = await runner.run(scenario)
    replies = [r for t in result.turns for r in t.agent]
    assert result.final_state == "handoff"
    assert result.handoff_reasons == ["quote_unavailable"]
    assert not any("/mês" in r for r in replies)
    assert result.quote_statuses == ["unavailable"]


async def test_rerunning_against_the_same_database_starts_fresh(
    settings: Settings, legacy_transport: httpx.ASGITransport
) -> None:
    scenario = next(s for s in SCENARIOS if s.name == "asks_for_human")
    first = await SimulationRunner(settings, legacy_transport, create_tables=True).run(scenario)
    second = await SimulationRunner(settings, legacy_transport, create_tables=True).run(scenario)
    assert first.passed
    assert second.passed
    assert first.conversation_id != second.conversation_id
