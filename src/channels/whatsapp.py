"""WhatsApp Cloud API adapter (webhook parsing, signature check, outbound senders).

The payload models follow Meta's webhook format, so the same endpoint works with a real
WhatsApp Business number. Without an access token the simulated sender is used: it logs
the (masked) outbound text and keeps the last messages in memory for inspection.
"""

from __future__ import annotations

import hashlib
import hmac
from collections import defaultdict, deque
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, Field

from src.agent.orchestrator import InboundMessage
from src.domain.models import MessageType
from src.utils.logging import get_logger
from src.utils.pii import mask_text

log = get_logger(__name__)

CHANNEL = "whatsapp"


class _Text(BaseModel):
    body: str = ""


class _Media(BaseModel):
    caption: str | None = None
    filename: str | None = None


class _Message(BaseModel):
    id: str
    from_: str = Field(alias="from")
    timestamp: str | None = None
    type: str = "text"
    text: _Text | None = None
    image: _Media | None = None
    audio: _Media | None = None
    document: _Media | None = None


class _Profile(BaseModel):
    name: str | None = None


class _Contact(BaseModel):
    wa_id: str
    profile: _Profile = Field(default_factory=_Profile)


class _Value(BaseModel):
    messaging_product: str = "whatsapp"
    contacts: list[_Contact] = Field(default_factory=list)
    messages: list[_Message] = Field(default_factory=list)


class _Change(BaseModel):
    field: str = "messages"
    value: _Value


class _Entry(BaseModel):
    id: str | None = None
    changes: list[_Change] = Field(default_factory=list)


class WebhookPayload(BaseModel):
    object: str = "whatsapp_business_account"
    entry: list[_Entry] = Field(default_factory=list)


_TYPES = {t.value: t for t in MessageType}


def parse_webhook(payload: WebhookPayload) -> list[InboundMessage]:
    """Flattens a webhook delivery into inbound messages (status callbacks are ignored)."""
    out: list[InboundMessage] = []
    for entry in payload.entry:
        for change in entry.changes:
            names = {c.wa_id: c.profile.name for c in change.value.contacts}
            for m in change.value.messages:
                mtype = _TYPES.get(m.type, MessageType.OTHER)
                text = m.text.body if m.text else ""
                received = (
                    datetime.fromtimestamp(int(m.timestamp), UTC)
                    if m.timestamp and m.timestamp.isdigit()
                    else None
                )
                out.append(
                    InboundMessage(
                        channel=CHANNEL,
                        channel_message_id=m.id,
                        contact_address=m.from_,
                        contact_name=names.get(m.from_),
                        message_type=mtype,
                        text=text,
                        received_at=received,
                    )
                )
    return out


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    """Validates `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the app secret)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


class SimulatedWhatsAppSender:
    def __init__(self, keep_last: int = 50) -> None:
        self._sent: defaultdict[str, deque[str]] = defaultdict(lambda: deque(maxlen=keep_last))

    async def send(self, contact_address: str, text: str) -> None:
        self._sent[contact_address].append(text)
        log.info("outbound_message", channel=CHANNEL, simulated=True, body=mask_text(text))

    def sent_to(self, contact_address: str) -> list[str]:
        return list(self._sent[contact_address])


class WhatsAppCloudSender:
    """Real sender (Graph API). Not exercised against Meta in this project's CI."""

    def __init__(
        self, client: httpx.AsyncClient, base_url: str, phone_number_id: str, token: str
    ) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/{phone_number_id}/messages"
        self._token = token

    async def send(self, contact_address: str, text: str) -> None:
        response = await self._client.post(
            self._url,
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "messaging_product": "whatsapp",
                "to": contact_address,
                "type": "text",
                "text": {"body": text},
            },
            timeout=10.0,
        )
        response.raise_for_status()
        log.info("outbound_message", channel=CHANNEL, simulated=False, body=mask_text(text))
