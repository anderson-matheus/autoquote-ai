# Development guide

## 1. Setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker (for PostgreSQL
tests and the stack).

```bash
make install        # dependencies (all groups) + pre-commit hooks
make test-fast      # unit tests, < 2 s
make check          # every gate CI runs (except image scan and stack smoke)
```

Pre-commit runs ruff, ruff-format, mypy, gitleaks and file hygiene hooks on each commit.

## 2. Conventions

| Topic | Rule |
|---|---|
| Language | Code, comments, docs, logs and commit messages in **English**. Only customer-facing copy (`src/agent/responses.py`) is pt-BR |
| Legacy contract | Field names of the quote API (`plano_id`, `idade`, …) are an external contract. Keep them as they are and translate at the adapter (`tools/quote_client.py`) |
| Layers | Respect the import contracts (`make arch`). Business rules go in `domain/` or `agent/` and never import infrastructure |
| Prices | Must only come from a `Quote` returned by the API. Never compute, cache or estimate a price |
| PII | Never log raw message text on purpose. Masking is a safety net, not a licence |
| Commits | Conventional Commits (`feat:`, `fix:`, `docs:`, `ci:`, `build:`, `chore:`…), atomic |
| Branching | Short-lived branches → PR → all checks green → rebase merge (`main` is protected) |
| Decisions | Significant design changes get an ADR in `docs/adr/` |

## 3. Project layout

See the [module map](architecture.md#5-module-map).

## 4. Recipes

### Add or improve an extraction rule

1. Add the pattern in `src/agent/extraction.py`. Rules work on accent-stripped,
   lower-cased text (`agent/text.py::normalize`) with PII spans removed.
2. Add parametrised cases to `tests/unit/test_extraction.py`, including a negative case
   (what must *not* match).
3. Run `make eval` to check the dataset accuracy did not regress.

### Add an intent

1. Add it to `Intent` and to `_INTENT_RULES` (order matters: the first match wins).
2. Handle it in `ConversationFSM` (`src/agent/fsm.py`), or in `HandoffPolicy` if it
   triggers a handoff.
3. Add the reply copy to `responses.py`, if new.
4. Update the LLM prompt (`prompts.py`) and bump `EXTRACTION_PROMPT_VERSION`.
5. Add unit tests for the FSM, plus a scenario in `src/simulation/scenarios.yaml`.

### Add a handoff reason

1. Add it to `HandoffReason` (`domain/models.py`).
2. Add the rule to `HandoffPolicy` and to its docstring table.
3. Add the copy in `responses._HANDOFF`. `test_every_handoff_reason_has_copy` fails until
   you do.
4. Document it in [handoff-criteria.md](handoff-criteria.md).

### Add a collected field

1. Add it to `LeadProfile` and `LeadField` (`domain/models.py`), and to
   `REQUIRED_FIELDS` if mandatory.
2. Add a validator in `domain/validators.py` and apply it in `ConversationFSM._apply_fields`.
3. Add the question to `responses._FIELD_QUESTIONS`.
4. Extract it in `extraction.py`. If it is personal data, encrypt it in
   `SqlUnitOfWork._profile_to_json` and mask it in logs.
5. If it changes the price, add it to `LeadProfile.quote_fingerprint` and
   `build_quote_payload`.

### Use another LLM provider

No code change: set `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` to any
OpenAI-compatible endpoint (examples in `.env.example`). For a non-compatible API,
implement the `LLMClient` protocol (`src/llm/port.py`) and wire it in `bootstrap.py`.

### Add a channel (e.g. Telegram, web chat)

1. Create `src/channels/<name>.py` that parses the inbound payload into `InboundMessage`
   and implements `OutboundChannel.send()`.
2. Add a route in `src/api/routes/`. Verify the signature, ack fast, and process in the
   background.
3. Wire the sender in `bootstrap.py`. The orchestrator and the FSM stay unchanged.

### Change the database schema

Edit `src/infra/db/models.py`, run
`uv run alembic revision --autogenerate -m "..."`, review the file, then run
`make migrations-check`.

### Add a scenario

Append to `src/simulation/scenarios.yaml`: `messages` (strings, or `{type, text}` for
media), optional `faults.quote_failures`, `run_requote_worker`, and `expect`
(`final_state`, `handoff_reason`, `quoted`). It runs in `make simulate` and in the E2E
suite automatically.

## 5. Tools

| Task | Command |
|---|---|
| Download the dataset | `make dataset` |
| Score the extractor on the dataset | `make eval` |
| Export sanitized AI sessions | `make ai-logs` |
| Scan the production image | `make scan` |
| Check architecture contracts | `make arch` |
