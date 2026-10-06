"""Background job: re-tries quotes for leads handed off because the quote API was down.

Why: an outage should not cost the sale. If the API recovers before a seller picks the
conversation up, the lead receives the quote automatically and the ticket is closed.
"""

from __future__ import annotations

import asyncio
import contextlib

from src.agent.orchestrator import Orchestrator
from src.domain.ports import UnitOfWorkFactory
from src.utils.logging import clear_context, get_logger

log = get_logger(__name__)


class RequoteWorker:
    def __init__(
        self,
        orchestrator: Orchestrator,
        uow_factory: UnitOfWorkFactory,
        interval_s: float,
        max_age_s: float,
        batch_size: int = 20,
    ) -> None:
        self._orchestrator = orchestrator
        self._uow_factory = uow_factory
        self._interval = interval_s
        self._max_age = max_age_s
        self._batch = batch_size
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> int:
        async with self._uow_factory() as uow:
            candidates = await uow.requote_candidates(self._max_age, self._batch)
        recovered = 0
        for conversation_id in candidates:
            try:
                recovered += int(await self._orchestrator.requote(conversation_id))
            except Exception:
                log.exception("requote_failed", conversation_id=conversation_id)
            finally:
                clear_context()
        if candidates:
            log.info("requote_cycle", candidates=len(candidates), recovered=recovered)
        return recovered

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="requote-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.run_once()
            except Exception:
                log.exception("requote_cycle_failed")
