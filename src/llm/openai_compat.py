"""Client for any OpenAI-compatible chat-completions endpoint.

Works with free tiers such as Groq, OpenRouter (`:free` models), Google Gemini's
OpenAI-compatible endpoint, or a local Ollama, selected purely through settings.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from src.llm.port import LLMUnavailableError
from src.tools.resilience import CircuitBreaker, CircuitOpenError
from src.utils.logging import get_logger

log = get_logger(__name__)


class OpenAICompatibleClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float,
        breaker: CircuitBreaker,
    ) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_s
        self._breaker = breaker
        self.name = f"openai_compat:{model}"

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        try:
            self._breaker.acquire()
        except CircuitOpenError as exc:
            raise LLMUnavailableError(str(exc)) from exc
        try:
            response = await self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            raise LLMUnavailableError(f"transport error: {exc!r}") from exc

        if response.status_code != 200:
            self._breaker.record_failure()
            log.warning("llm_http_error", status_code=response.status_code, model=self._model)
            raise LLMUnavailableError(f"http {response.status_code}")
        self._breaker.record_success()
        try:
            content = response.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMUnavailableError(f"invalid completion payload: {exc!r}") from exc
        if not isinstance(data, dict):
            raise LLMUnavailableError("completion is not a JSON object")
        return data
