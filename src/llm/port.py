"""LLM port. The agent only asks LLMs for *structured extraction*, never for prices."""

from __future__ import annotations

from typing import Any, Protocol


class LLMUnavailableError(Exception):
    """Rate-limited, timed out, misconfigured or returned invalid JSON."""


class LLMClient(Protocol):
    name: str

    async def complete_json(self, system: str, user: str) -> dict[str, Any]: ...
