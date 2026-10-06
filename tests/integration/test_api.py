from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from src.api.app import create_app
from src.bootstrap import build_container
from src.config import Settings
from src.infra.db.session import create_schema


def wa_payload(text: str, mid: str, wa_id: str = "5511977776666") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": wa_id, "profile": {"name": "Ana"}}],
                            "messages": [
                                {"id": mid, "from": wa_id, "type": "text", "text": {"body": text}}
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def _client(
    settings: Settings, legacy: httpx.ASGITransport
) -> AsyncIterator[httpx.AsyncClient]:
    container = build_container(settings, quote_transport=legacy)
    await create_schema(container.engine)
    app = create_app(container)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            yield client
    await container.aclose()


@pytest.fixture
async def client(
    settings: Settings, legacy_transport: httpx.ASGITransport
) -> AsyncIterator[httpx.AsyncClient]:
    settings = settings.model_copy(update={"admin_api_key": SecretStr("adm")})
    async for c in _client(settings, legacy_transport):
        yield c


async def test_health_and_ready(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    ready = (await client.get("/ready")).json()
    assert ready["checks"]["database"] == "ok"
    assert ready["checks"]["circuits"] == {"quote-api": "closed", "catalog-api": "closed"}
    assert ready["checks"]["catalog_cached"] is True


async def test_verification_handshake(client: httpx.AsyncClient) -> None:
    ok = await client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "dev-verify-token",
            "hub.challenge": "42",
        },
    )
    assert (ok.status_code, ok.text) == (200, "42")
    bad = await client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"},
    )
    assert bad.status_code == 403


async def test_conversation_over_webhook_with_audit_timeline(client: httpx.AsyncClient) -> None:
    messages = ["oi", "Corolla 2022, tenho 35 anos, cep 01310-100, começar hoje", "completo"]
    result: dict[str, Any] = {}
    for i, text in enumerate(messages):
        r = await client.post(
            "/webhooks/whatsapp",
            params={"sync": "true"},
            json=wa_payload(text, f"wamid.api.{i}"),
            headers={"X-Trace-Id": f"trace-api-{i:04d}"},
        )
        assert r.status_code == 200
        assert r.headers["X-Trace-Id"] == f"traceapi{i:04d}"
        result = r.json()["results"][0]
    assert result["state"] == "presenting"
    assert "R$ 209,90/mês" in result["replies"][-1]

    dup = await client.post(
        "/webhooks/whatsapp", params={"sync": "true"}, json=wa_payload("completo", "wamid.api.2")
    )
    assert dup.json()["results"][0]["duplicate"] is True

    cid = result["conversation_id"]
    assert (await client.get(f"/conversations/{cid}")).status_code == 401
    timeline = (await client.get(f"/conversations/{cid}", headers={"X-Api-Key": "adm"})).json()
    assert timeline["state"] == "presenting"
    assert [q["status"] for q in timeline["quotes"]] == ["success"]
    assert timeline["quotes"][0]["request"]["cep"] == "01310-***"
    assert all("01310-100" not in m["body"] for m in timeline["messages"])
    assert (
        await client.get("/conversations/nope", headers={"X-Api-Key": "adm"})
    ).status_code == 404


async def test_async_mode_acknowledges_immediately(client: httpx.AsyncClient) -> None:
    r = await client.post("/webhooks/whatsapp", json=wa_payload("oi", "wamid.async"))
    assert r.json()["received"] == 1
    assert "results" not in r.json()


async def test_invalid_payload(client: httpx.AsyncClient) -> None:
    r = await client.post("/webhooks/whatsapp", content=b"{not json")
    assert r.status_code == 422


async def test_signature_enforced_when_secret_configured(
    settings: Settings, legacy_transport: httpx.ASGITransport
) -> None:
    secured = settings.model_copy(update={"whatsapp_app_secret": SecretStr("app-secret")})
    async for client in _client(secured, legacy_transport):
        body = json.dumps(wa_payload("oi", "wamid.sig")).encode()
        unsigned = await client.post("/webhooks/whatsapp", content=body)
        assert unsigned.status_code == 401
        sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        signed = await client.post(
            "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig}
        )
        assert signed.status_code == 200
