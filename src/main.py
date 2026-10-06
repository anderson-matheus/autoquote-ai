"""Command-line entry point.

python -m src.main --serve                 # HTTP API (webhook) on :8080
python -m src.main --run-simulation        # scripted end-to-end conversations
python -m src.main --interactive           # chat with the agent in the terminal
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

import uvicorn

from src.agent.orchestrator import InboundMessage
from src.bootstrap import build_container
from src.channels.whatsapp import CHANNEL
from src.config import Settings, get_settings
from src.domain.models import MessageType
from src.infra.db.session import create_schema
from src.simulation.runner import (
    SimulationRunner,
    load_scenarios,
    render_transcript,
    sqlite_url,
)
from src.utils.logging import bind_trace_id, clear_context, configure_logging


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve", action="store_true", help="run the HTTP API")
    mode.add_argument("--run-simulation", action="store_true", help="run scripted scenarios")
    mode.add_argument("--interactive", action="store_true", help="chat in the terminal")
    parser.add_argument("--scenario", action="append", help="only run these scenario names")
    parser.add_argument("--scenarios-file", type=Path, help="alternative scenarios YAML")
    parser.add_argument("--transcript-out", type=Path, help="also write the transcript here")
    parser.add_argument(
        "--sqlite", action="store_true", help="use a throwaway SQLite DB instead of DATABASE_URL"
    )
    # Binding all interfaces is required inside the container.
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--port", type=int, default=8080)
    return parser.parse_args(argv)


def _settings(args: argparse.Namespace) -> Settings:
    settings = get_settings()
    if args.sqlite:
        settings = settings.model_copy(update={"database_url": sqlite_url()})
    return settings


async def _simulate(args: argparse.Namespace, settings: Settings) -> int:
    scenarios = load_scenarios(args.scenarios_file)
    if args.scenario:
        scenarios = [s for s in scenarios if s.name in set(args.scenario)]
    runner = SimulationRunner(settings, create_tables=args.sqlite)
    results = [await runner.run(s) for s in scenarios]
    transcript = render_transcript(results)
    print(transcript)
    if args.transcript_out:
        args.transcript_out.parent.mkdir(parents=True, exist_ok=True)
        args.transcript_out.write_text(transcript + "\n", encoding="utf-8")
    return 0 if all(r.passed for r in results) else 1


async def _interactive(settings: Settings, sqlite: bool) -> int:
    container = build_container(settings)
    if sqlite:
        await create_schema(container.engine)
    contact = f"cli-{uuid.uuid4().hex[:8]}"
    print("Chat com o agente (Ctrl+D para sair)")
    try:
        while True:
            try:
                text = await asyncio.to_thread(input, "você > ")
            except EOFError:
                return 0
            clear_context()
            bind_trace_id()
            result = await container.orchestrator.handle(
                InboundMessage(
                    CHANNEL, uuid.uuid4().hex, contact, "Cliente CLI", MessageType.TEXT, text
                )
            )
            for reply in result.replies:
                print(f"agente < {reply}")
            print(f"        [state: {result.state}]")
    finally:
        await container.aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    settings = _settings(args)
    if args.serve:
        uvicorn.run(
            "src.api.app:app_factory", factory=True, host=args.host, port=args.port, log_config=None
        )
        return 0
    # Keep the terminal readable: JSON logs go to the log file only.
    configure_logging(settings.log_level, settings.log_file, stream=False)
    if args.run_simulation:
        return asyncio.run(_simulate(args, settings))
    return asyncio.run(_interactive(settings, args.sqlite))


if __name__ == "__main__":
    sys.exit(main())
