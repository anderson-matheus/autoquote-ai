"""Structured JSON logging with trace ids and mandatory PII masking.

Every event goes through `_mask_pii` before rendering, so a developer who logs a raw
message by mistake still does not leak CPF/phone/e-mail to stdout or `logs/`.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import uuid
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import structlog

from src.utils.pii import mask_value

_RESERVED = frozenset({"event", "level", "timestamp", "logger", "trace_id"})


def _mask_pii(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key, value in list(event_dict.items()):
        if key in _RESERVED and key != "event":
            continue
        event_dict[key] = mask_value(value, key)
    return event_dict


def configure_logging(
    level: str = "INFO", log_file: str | None = None, *, stream: bool = True
) -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _mask_pii,
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)] if stream else []
    file_error: OSError | None = None
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
                )
            )
        except OSError as exc:  # e.g. read-only volume: keep logging to stdout
            file_error = exc
            if not handlers:
                handlers.append(logging.StreamHandler(sys.stderr))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "uvicorn.access", "aiosqlite", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    if file_error is not None:
        get_logger(__name__).warning("log_file_unavailable", error=str(file_error))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def new_trace_id() -> str:
    return uuid.uuid4().hex


def bind_trace_id(trace_id: str | None = None) -> str:
    tid = trace_id or new_trace_id()
    structlog.contextvars.bind_contextvars(trace_id=tid)
    return tid


def current_trace_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("trace_id")
    return value if isinstance(value, str) else None


def bind_context(**values: Any) -> None:
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
