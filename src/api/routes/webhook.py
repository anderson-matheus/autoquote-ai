"""WhatsApp Cloud API webhook (verification handshake + message delivery)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from src.agent.orchestrator import InboundMessage, Orchestrator
from src.api.dependencies import ContainerDep
from src.channels.whatsapp import WebhookPayload, parse_webhook, verify_signature
from src.utils.logging import bind_trace_id, clear_context, current_trace_id, get_logger

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])
log = get_logger(__name__)


@router.get("", response_class=PlainTextResponse)
async def verify(
    container: ContainerDep,
    mode: Annotated[str, Query(alias="hub.mode")],
    token: Annotated[str, Query(alias="hub.verify_token")],
    challenge: Annotated[str, Query(alias="hub.challenge")],
) -> str:
    expected = container.settings.whatsapp_verify_token.get_secret_value()
    if mode != "subscribe" or token != expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")
    return challenge


@router.post("", status_code=status.HTTP_200_OK)
async def receive(
    request: Request,
    container: ContainerDep,
    background: BackgroundTasks,
    sync: Annotated[bool, Query(description="Process inline and return replies (dev)")] = False,
) -> dict[str, object]:
    body = await request.body()
    secret = container.settings.whatsapp_app_secret
    if secret is not None and not verify_signature(
        body, request.headers.get("X-Hub-Signature-256"), secret.get_secret_value()
    ):
        log.warning("webhook_signature_invalid")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    try:
        payload = WebhookPayload.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid payload") from exc

    messages = parse_webhook(payload)
    trace_id = current_trace_id() or bind_trace_id()
    if sync and container.settings.environment != "production":
        results = [await _process(container.orchestrator, m, trace_id) for m in messages]
        return {"received": len(messages), "trace_id": trace_id, "results": results}
    # Meta expects a fast 200; the agent may take seconds (quote retries), so the work
    # runs after the response. Redeliveries are absorbed by message-id idempotency.
    for m in messages:
        background.add_task(_process, container.orchestrator, m, trace_id)
    return {"received": len(messages), "trace_id": trace_id}


async def _process(
    orchestrator: Orchestrator, message: InboundMessage, trace_id: str
) -> dict[str, object]:
    clear_context()
    bind_trace_id(trace_id)
    try:
        result = await orchestrator.handle(message)
    except Exception:
        log.exception("message_processing_failed")
        raise
    finally:
        clear_context()
    return {
        "conversation_id": result.conversation_id,
        "state": result.state.value if result.state else None,
        "duplicate": result.duplicate,
        "replies": list(result.replies),
    }
