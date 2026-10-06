# ADR 0001 — Hand-written state machine instead of an agent framework

**Status:** accepted

## Context
The guide mentions "transition graphs" (`src/agent`). LangGraph and similar frameworks
model agents as graphs in which an LLM drives the transitions. In this domain, almost
every transition is a business rule: required fields, underwriting limits, when to hand
off. These rules must be deterministic, auditable and testable.

## Decision
Use a pure, hand-written FSM (`src/agent/fsm.py`):
- `ConversationFSM.on_message / on_media / on_quote_result` → `Decision(snapshot, replies, effects)`;
- no I/O inside the FSM. Effects (`RequestQuote`, `OpenHandoff`) run in the orchestrator,
  which feeds their results back into the FSM;
- the LLM only does extraction. It never decides transitions and never produces prices.

## Consequences
- Every transition is covered by fast unit tests without mocks (`tests/unit/test_fsm.py`).
- Behaviour is reproducible and explainable: a log line `state_transition` plus the
  extraction result says exactly why something happened.
- No framework lock-in and no extra dependency. The cost is that new conversational
  skills (for example FAQ answers) have to be added as explicit states or intents.
