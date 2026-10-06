from __future__ import annotations

import json

import httpx
import pytest
import respx

from src.llm.openai_compat import OpenAICompatibleClient
from src.llm.port import LLMUnavailableError
from src.tools.resilience import CircuitBreaker, CircuitState
from tests.conftest import FakeClock

URL = "https://llm.test/v1/chat/completions"


def client(clock: FakeClock) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        httpx.AsyncClient(),
        "https://llm.test/v1",
        "key",
        "m",
        5.0,
        CircuitBreaker("llm", 2, 60, clock),
    )


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@respx.mock
async def test_returns_parsed_json_and_sends_json_mode(clock: FakeClock) -> None:
    route = respx.post(URL).mock(return_value=completion('{"intent": "accept"}'))
    assert await client(clock).complete_json("sys", "user") == {"intent": "accept"}
    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["temperature"] == 0
    assert route.calls[0].request.headers["Authorization"] == "Bearer key"


@respx.mock
async def test_rate_limit_opens_breaker(clock: FakeClock) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(429))
    c = client(clock)
    for _ in range(2):
        with pytest.raises(LLMUnavailableError, match="429"):
            await c.complete_json("s", "u")
    with pytest.raises(LLMUnavailableError, match="open"):
        await c.complete_json("s", "u")
    assert route.call_count == 2
    assert c._breaker.state is CircuitState.OPEN


@respx.mock
@pytest.mark.parametrize(
    "response",
    [completion("not json"), completion("[1, 2]"), httpx.Response(200, json={"x": 1})],
)
async def test_invalid_payloads(clock: FakeClock, response: httpx.Response) -> None:
    respx.post(URL).mock(return_value=response)
    with pytest.raises(LLMUnavailableError):
        await client(clock).complete_json("s", "u")


@respx.mock
async def test_transport_error(clock: FakeClock) -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("x"))
    with pytest.raises(LLMUnavailableError, match="transport"):
        await client(clock).complete_json("s", "u")
