# ADR 0003 — PostgreSQL, one transaction per message, row lock, idempotency

**Status:** accepted

## Context
WhatsApp re-delivers webhooks, and a lead often sends several messages in a row. Two
workers processing messages of the same conversation at the same time could both read
state `PLAN_SELECTION` and both call `/quote`, or overwrite each other's snapshot.

## Decision
- Each inbound message is processed in **one transaction**. The conversation row is read
  with `SELECT … FOR UPDATE`, which serialises the messages of a lead without blocking
  other leads.
- `messages.channel_message_id` is `UNIQUE` and checked first, so a re-delivered webhook
  is a no-op.
- Creating a conversation is race-safe: a `UNIQUE` contact hash plus a SAVEPOINT, then
  re-read.
- Alembic migrations. CI checks that the models and migrations match (`alembic check`).

## Consequences
- The lock is held during the quote call (up to the 12 s budget). That only delays the
  *same* lead's next message, which is the behaviour we want. With very high per-lead
  concurrency an outbox/queue would be the next step (see README "next steps").
- SQLite is supported only for tests and the local simulation, with SQLAlchemy's recipe
  for correct SAVEPOINT semantics.
