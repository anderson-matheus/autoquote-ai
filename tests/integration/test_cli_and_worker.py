from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src import main as cli
from src.agent.requote_worker import RequoteWorker
from src.config import Settings


def test_cli_requires_a_mode() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_cli_simulation_writes_transcript(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Quote API unreachable on purpose: the chosen scenario never needs a price.
    unreachable = settings.model_copy(update={"quote_service_url": "http://127.0.0.1:9"})
    monkeypatch.setattr(cli, "get_settings", lambda: unreachable)
    out = tmp_path / "transcript.txt"
    code = cli.main(
        [
            "--run-simulation",
            "--sqlite",
            "--scenario",
            "asks_for_human",
            "--transcript-out",
            str(out),
        ]
    )
    assert code == 0
    assert "1/1 scenarios passed" in out.read_text()


class _Uow:
    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def requote_candidates(self, max_age_s: float, limit: int) -> list[str]:
        return ["c1", "c2"]


class _Orchestrator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def requote(self, conversation_id: str) -> bool:
        self.calls.append(conversation_id)
        if conversation_id == "c1":
            raise RuntimeError("boom")
        return True


async def test_worker_isolates_failures_and_runs_in_background() -> None:
    orch = _Orchestrator()
    worker = RequoteWorker(orch, _Uow, interval_s=0.01, max_age_s=60)  # type: ignore[arg-type]
    assert await worker.run_once() == 1  # c1 failed, c2 recovered, the cycle survived
    worker.start()
    worker.start()  # idempotent
    await asyncio.sleep(0.05)
    await worker.stop()
    await worker.stop()
    assert len(orch.calls) > 2
