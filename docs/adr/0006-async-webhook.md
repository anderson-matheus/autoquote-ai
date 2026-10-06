# ADR 0006 — Acknowledge the webhook immediately, process after the response

**Status:** accepted

## Context
Meta expects a fast `200` on webhook deliveries and re-delivers otherwise. A turn can
take seconds (quote retries).

## Decision
`POST /webhooks/whatsapp` validates the signature (`X-Hub-Signature-256` when
`WHATSAPP_APP_SECRET` is set), parses the payload, schedules processing as a background
task and returns `200`. Re-deliveries are absorbed by the `channel_message_id`
idempotency (ADR 0003). `?sync=true` runs the turn inline and returns the replies. It is
for local development and demos only and is disabled in production.

## Consequences
In-process background tasks are lost if the process dies mid-turn. The next step for
scale is a durable queue (Redis Streams, SQS or Pub/Sub) between the webhook and the
orchestrator. The orchestrator is already transport-agnostic, so this is a wiring change.
