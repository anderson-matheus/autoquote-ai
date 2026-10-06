from __future__ import annotations

import hashlib
import hmac

from src.channels.whatsapp import (
    SimulatedWhatsAppSender,
    WebhookPayload,
    parse_webhook,
    verify_signature,
)
from src.domain.models import MessageType


def payload(*messages: dict[str, object]) -> WebhookPayload:
    return WebhookPayload.model_validate(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "contacts": [
                                    {"wa_id": "5511999990000", "profile": {"name": "Ana Souza"}}
                                ],
                                "messages": list(messages),
                            },
                        }
                    ],
                }
            ],
        }
    )


def test_parses_text_and_media_messages() -> None:
    parsed = parse_webhook(
        payload(
            {
                "id": "wamid.1",
                "from": "5511999990000",
                "timestamp": "1760000000",
                "type": "text",
                "text": {"body": "oi"},
            },
            {"id": "wamid.2", "from": "5511999990000", "type": "audio", "audio": {}},
            {"id": "wamid.3", "from": "5511999990000", "type": "sticker"},
        )
    )
    assert [(m.channel_message_id, m.message_type, m.text) for m in parsed] == [
        ("wamid.1", MessageType.TEXT, "oi"),
        ("wamid.2", MessageType.AUDIO, ""),
        ("wamid.3", MessageType.OTHER, ""),
    ]
    assert parsed[0].contact_name == "Ana Souza"
    assert parsed[0].received_at is not None


def test_status_callbacks_produce_no_messages() -> None:
    assert parse_webhook(payload()) == []


def test_signature() -> None:
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, "secret")
    assert not verify_signature(body, sig, "other")
    assert not verify_signature(body, None, "secret")
    assert not verify_signature(body, "md5=abc", "secret")


async def test_simulated_sender_keeps_history() -> None:
    sender = SimulatedWhatsAppSender(keep_last=2)
    for text in ("a", "b", "c"):
        await sender.send("x", text)
    assert sender.sent_to("x") == ["b", "c"]
