"""Runs scripted conversations through the real agent stack and checks expectations.

Used by `python -m src.main --run-simulation` (demo + reference log) and the E2E tests.
"""

from __future__ import annotations

import asyncio
import itertools
import tempfile
import uuid
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import select

from src.agent.orchestrator import InboundMessage
from src.bootstrap import Container, build_container
from src.channels.whatsapp import CHANNEL, SimulatedWhatsAppSender
from src.config import Settings
from src.domain.models import MessageType
from src.infra.db.models import HandoffRow, QuoteRow
from src.infra.db.session import create_schema
from src.simulation.faults import FaultInjectingTransport
from src.utils.logging import bind_context, bind_trace_id, clear_context, get_logger
from src.utils.pii import mask_text

log = get_logger(__name__)

_RECOVERY_CYCLES = 8  # P(all probes fail at 30% chaos) ≈ 0.3**8 < 0.01%
_DEMO_BREAKER_RECOVERY_S = 1.0


@dataclass(frozen=True, slots=True)
class ScriptedMessage:
    text: str
    type: MessageType = MessageType.TEXT


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    contact: str
    contact_name: str
    messages: tuple[ScriptedMessage, ...]
    expect: dict[str, Any]
    quote_failures: int = 0
    run_requote_worker: bool = False


@dataclass(slots=True)
class Turn:
    lead: str
    agent: list[str]
    state: str | None


@dataclass(slots=True)
class ScenarioResult:
    scenario: Scenario
    conversation_id: str | None = None
    turns: list[Turn] = field(default_factory=list)
    final_state: str | None = None
    handoff_reasons: list[str] = field(default_factory=list)
    handoff_statuses: list[str] = field(default_factory=list)
    quote_statuses: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def load_scenarios(path: Path | None = None) -> list[Scenario]:
    raw_text = (
        path.read_text(encoding="utf-8")
        if path
        else resources.files("src.simulation").joinpath("scenarios.yaml").read_text("utf-8")
    )
    scenarios = []
    for item in yaml.safe_load(raw_text):
        messages = tuple(
            ScriptedMessage(m)
            if isinstance(m, str)
            else ScriptedMessage(m.get("text", ""), MessageType(m["type"]))
            for m in item["messages"]
        )
        scenarios.append(
            Scenario(
                name=item["name"],
                description=" ".join(str(item.get("description", "")).split()),
                contact=str(item["contact"]),
                contact_name=item.get("contact_name", ""),
                messages=messages,
                expect=item.get("expect", {}),
                quote_failures=int(item.get("faults", {}).get("quote_failures", 0)),
                run_requote_worker=bool(item.get("run_requote_worker", False)),
            )
        )
    return scenarios


class SimulationRunner:
    def __init__(
        self,
        settings: Settings,
        quote_transport: httpx.AsyncBaseTransport | None = None,
        *,
        create_tables: bool = False,
        run_tag: str | None = None,
    ) -> None:
        self._settings = settings
        # Unique contacts per run, so re-running against the same database starts fresh.
        self._run_tag = run_tag if run_tag is not None else uuid.uuid4().hex[:6]
        self._transport = quote_transport
        self._create_tables = create_tables
        self._ids = itertools.count(1)

    async def run(self, scenario: Scenario) -> ScenarioResult:
        transport = FaultInjectingTransport(
            self._transport or httpx.AsyncHTTPTransport(), scenario.quote_failures
        )
        sender = SimulatedWhatsAppSender()
        settings = self._settings
        if scenario.run_requote_worker:
            # Compress the breaker's recovery window so the demo does not wait 30 s.
            settings = settings.model_copy(
                update={"breaker_recovery_timeout_s": _DEMO_BREAKER_RECOVERY_S}
            )
        container = build_container(settings, outbound=sender, quote_transport=transport)
        try:
            if self._create_tables:
                await create_schema(container.engine)
            return await self._run(container, scenario, transport, sender)
        finally:
            clear_context()
            await container.aclose()

    async def _run(
        self,
        container: Container,
        scenario: Scenario,
        transport: FaultInjectingTransport,
        sender: SimulatedWhatsAppSender,
    ) -> ScenarioResult:
        result = ScenarioResult(scenario)
        contact = f"{scenario.contact}{self._run_tag}"
        bind_context(scenario=scenario.name)
        log.info("simulation_scenario_started", description=scenario.description)
        for msg in scenario.messages:
            bind_trace_id()
            turn = await container.orchestrator.handle(
                InboundMessage(
                    channel=CHANNEL,
                    channel_message_id=f"sim{self._run_tag}-{scenario.name}-{next(self._ids)}",
                    contact_address=contact,
                    contact_name=scenario.contact_name,
                    message_type=msg.type,
                    text=msg.text,
                )
            )
            result.conversation_id = turn.conversation_id or result.conversation_id
            result.turns.append(
                Turn(msg.text, list(turn.replies), turn.state.value if turn.state else None)
            )
            result.final_state = result.turns[-1].state

        if scenario.run_requote_worker and result.conversation_id:
            transport.remaining_failures = 0  # the legacy API "comes back"
            recovered = await self._run_worker_until_recovered(container)
            replies = sender.sent_to(contact)[-2:] if recovered else []
            result.turns.append(
                Turn("(background re-quote after the API recovered)", replies, None)
            )

        await self._collect_audit(container, result)
        self._check(result)
        log.info(
            "simulation_scenario_finished",
            passed=result.passed,
            final_state=result.final_state,
            failures=result.failures,
        )
        return result

    async def _run_worker_until_recovered(self, container: Container) -> int:
        """Background worker cycles, as in production, after the outage ends.

        The outage left the breaker one failure away from opening, and the legacy service
        still fails ~30% of calls at random, so a single cycle can legitimately trip the
        breaker. Like the real worker, keep cycling: each cycle after the (shortened)
        recovery window is a half-open probe."""
        recovered = 0
        for cycle in range(1, _RECOVERY_CYCLES + 1):
            recovered = await container.worker.run_once()
            log.info("simulation_requote_worker_ran", cycle=cycle, recovered=recovered)
            if recovered:
                break
            await asyncio.sleep(container.settings.breaker_recovery_timeout_s)
        return recovered

    async def _collect_audit(self, container: Container, result: ScenarioResult) -> None:
        if result.conversation_id is None:
            return
        cid = result.conversation_id
        async with container.uow() as uow:
            record = await uow.get_conversation(cid)
            result.final_state = record.snapshot.state.value if record else None
        async with container.sessions() as session:
            handoffs = (
                await session.execute(select(HandoffRow).where(HandoffRow.conversation_id == cid))
            ).scalars()
            for h in handoffs:
                result.handoff_reasons.append(h.reason)
                result.handoff_statuses.append(h.status)
            quotes = (
                await session.execute(select(QuoteRow).where(QuoteRow.conversation_id == cid))
            ).scalars()
            result.quote_statuses = [q.status for q in quotes]

    @staticmethod
    def _check(result: ScenarioResult) -> None:
        expect = result.scenario.expect
        if "final_state" in expect and result.final_state != expect["final_state"]:
            result.failures.append(
                f"final_state={result.final_state!r}, expected {expect['final_state']!r}"
            )
        reason = expect.get("handoff_reason")
        if reason and reason not in result.handoff_reasons:
            result.failures.append(f"handoff {reason!r} not opened ({result.handoff_reasons})")
        if "quoted" in expect and expect["quoted"] != ("success" in result.quote_statuses):
            result.failures.append(f"quoted expected {expect['quoted']} ({result.quote_statuses})")


def render_transcript(results: list[ScenarioResult]) -> str:
    lines: list[str] = []
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines += [
            f"=== [{status}] {r.scenario.name} ===",
            r.scenario.description,
            "",
        ]
        for t in r.turns:
            lines.append(f"  lead  > {mask_text(t.lead, [r.scenario.contact_name])}")
            for reply in t.agent:
                first, *rest = reply.splitlines()
                lines.append(f"  agent < {first}")
                lines.extend(f"          {line}" for line in rest)
            if t.state:
                lines.append(f"          [state: {t.state}]")
        handoffs = list(zip(r.handoff_reasons, r.handoff_statuses, strict=True))
        lines += [
            f"  final_state={r.final_state} handoffs={handoffs} quotes={r.quote_statuses}",
            *(f"  !! {f}" for f in r.failures),
            "",
        ]
    passed = sum(r.passed for r in results)
    lines.append(f"{passed}/{len(results)} scenarios passed")
    return "\n".join(lines)


def sqlite_url() -> str:
    path = Path(tempfile.mkdtemp(prefix="autoquote-sim-")) / "simulation.db"
    return f"sqlite+aiosqlite:///{path}"
