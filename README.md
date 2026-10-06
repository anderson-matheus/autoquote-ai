# AutoQuote AI

A WhatsApp agent for **AutoSeguro** (a fictitious car insurer). It talks to leads,
qualifies them, quotes a plan through a **deliberately unreliable legacy quote API**, and
decides, using explicit criteria, when to close by itself and when to hand the
conversation to a human seller. It never stalls and **never invents a price**.

Built for the Namastex FDE challenge
([reference repo](https://github.com/namastexlabs/namastex-fde-challenge)).

```
                     ┌────────────────────────────────┐
                     │  WhatsApp webhook (Cloud API)  │  POST /webhooks/whatsapp
                     └───────────────┬────────────────┘  signature check, 200 fast, idempotent
                                     ▼
                      ┌──────────────────────────────┐
                      │  Orchestrator (1 tx / msg)   │  row lock, trace_id, audit trail
                      │  extraction ► FSM ► effects  │  rules first, LLM only if inconclusive
                      └──────────────┬───────────────┘
                  ┌──────────────────┴──────────────────┐
         (missing / invalid data)                 (profile complete)
                  ▼                                     ▼
       ┌─────────────────────┐             ┌──────────────────────────┐
       │ Ask only what's     │             │ Underwriting pre-screen  │  rules from /planos
       │ missing, explain    │             │ PII masked everywhere    │
       │ invalid values      │             └────────────┬─────────────┘
       └─────────────────────┘                          ▼
                                           ┌──────────────────────────┐
                                           │ Resilient HTTP client    │  3 s timeout, 12 s budget
                                           │ backoff + full jitter    │  4 attempts, Retry-After
                                           │ circuit breaker          │  5 fails → open 30 s
                                           └────────────┬─────────────┘
                         ┌──────────────────────────────┴───────────────────────────┐
                  (quote returned)                                    (refused / unavailable / bug)
                         ▼                                                            ▼
             ┌───────────────────────┐   accept / 2nd objection   ┌─────────────────────────────┐
             │ Present plan (prices  │ ─────────────────────────► │ Handoff ticket + masked     │
             │ only from API JSON)   │                            │ summary; auto re-quote job  │
             └───────────────────────┘                            └─────────────────────────────┘
```

---

## Quick start (Docker, end to end)

**Prerequisites:** Docker with Compose v2.

### 1. Environment

```bash
git clone https://github.com/anderson-matheus/autoquote-ai.git && cd autoquote-ai
cp .env.example .env
```

Every variable has a working default. The important ones:

```env
LLM_API_KEY=                 # optional: Groq free-tier key (gsk_...). Empty = rules only, fully offline
QUOTE_SERVICE_URL=http://quote-service:8000
LOG_LEVEL=INFO
ENVIRONMENT=development      # production also requires PII_ENCRYPTION_KEY and ADMIN_API_KEY
```

### 2. Start the stack (PostgreSQL + legacy quote service + agent)

```bash
make up                      # = docker compose up --build -d (also sets AGENT_UID so logs/ is writable)
docker compose logs -f
```

Ports 8000 (quote service) and 8080 (agent) can be changed with `QUOTE_SERVICE_PORT` and
`AGENT_PORT`.

### 3. Health checks

```bash
curl -X GET http://localhost:8000/health     # legacy quote service
curl -X GET http://localhost:8000/planos     # plans + underwriting rules
curl http://localhost:8080/ready             # agent: DB, circuit-breaker states, catalog cache
```

### 4. Simulation and tests

```bash
# Scripted end-to-end conversations against the real stack (exit code ≠ 0 if any fails)
docker compose exec agent python -m src.main --run-simulation

# Full test suite inside the container (integration tests use the compose PostgreSQL)
docker compose exec agent pytest tests/

# Chat with the agent yourself
docker compose exec agent python -m src.main --interactive
```

Or send a real WhatsApp-shaped webhook (`?sync=true` returns the replies, dev only):

```bash
curl -s -X POST 'localhost:8080/webhooks/whatsapp?sync=true' -H 'content-type: application/json' -d '{
  "object":"whatsapp_business_account","entry":[{"changes":[{"value":{
  "contacts":[{"wa_id":"5511912345678","profile":{"name":"Ana"}}],
  "messages":[{"id":"wamid.1","from":"5511912345678","type":"text",
  "text":{"body":"oi, quero cotar meu Compass 2023, tenho 29 anos, cep 21040-360, começar hoje"}}]}}]}]}'
```

### 5. Audit and traceability

- `logs/execution.log`: structured JSON, one event per line, each with `trace_id` and
  `conversation_id`. Personal data is masked by the logging pipeline itself.
- `GET /conversations/{id}` (header `X-Api-Key` when `ADMIN_API_KEY` is set) rebuilds the
  whole timeline: messages (masked), every quote with **every HTTP attempt** (outcome,
  status, latency), and the handoff tickets.

**Reference runs** (committed, produced by the commands above):

| File | What it shows |
|---|---|
| [`logs/reference/transcript.txt`](logs/reference/transcript.txt) | 6 scenarios, default chaos (20% 5xx, 10% slow): retries absorbed, 6/6 pass |
| [`logs/reference/execution.log`](logs/reference/execution.log) | The JSON audit log of that run |
| [`logs/reference/high-chaos-transcript.txt`](logs/reference/high-chaos-transcript.txt) + [`.log`](logs/reference/high-chaos-execution.log) | 30% 5xx + 40% slow: timeouts, circuit opens, handoff `quote_unavailable`, **no invented price** |

---

## Local development

```bash
make install          # uv sync --all-groups + pre-commit hooks (Python 3.12, uv)
make check            # every CI gate: ruff, format, mypy --strict, bandit, pip-audit, tests + coverage ≥ 85%
make test-fast        # unit tests only (< 2 s)
make simulate-local   # scenarios with SQLite against QUOTE_SERVICE_URL
make dataset && make eval   # download the dataset and score the extractor on it
```

| Gate | Tool | Where |
|---|---|---|
| Lint + format | ruff (incl. bandit rules `S`, pylint `PL`, bugbear `B`) | pre-commit, CI |
| Types | mypy `--strict` (src + scripts) | pre-commit, CI |
| Security | bandit, pip-audit (known CVEs), gitleaks (secrets) | CI (gitleaks also in pre-commit) |
| Tests | 220+ tests: unit, integration (real PostgreSQL via `TEST_DATABASE_URL` or testcontainers), contract, E2E | CI, `make test` |
| Coverage | branch coverage, `fail_under = 85` (currently ~95%) | CI |
| Warnings | `filterwarnings = error` | pytest |
| Migrations | `alembic check`: models == migrations | CI |
| Stack smoke | `docker compose up --wait`, health, simulation, tests in container, PII grep on the log | CI |

---

## How it works

### Conversation flow
A pure state machine (`src/agent/fsm.py`, [ADR 0001](docs/adr/0001-own-state-machine.md)):

`NEW → COLLECTING → PLAN_SELECTION → (quote) → PRESENTING → HANDOFF | CLOSED`

- It asks **only for what is missing**: vehicle model and year, driver age, CEP, start date.
  Leads usually send several fields in one message. Invalid values are explained, not
  silently dropped.
- Plans come from `/planos` (cached, stale-while-error). The underwriting rules in the
  same payload **pre-screen** the lead (vehicle > 20 years, driver > 75), so no quote call
  is wasted on a guaranteed refusal.
- The quote is presented **only from the API's JSON**: monthly premium, deductible,
  coverages, the **30-day waiting period for theft/robbery**, and the **pro-rata first
  payment** when coverage starts mid-month. A test asserts that no template can render a
  monthly price without a `Quote` object.
- In `PRESENTING`: plan or data changes trigger a re-quote. On the first price objection
  the agent offers the next cheaper plan, and "pode ser" quotes it. Accepting leads to a
  closing handoff. Declining closes the conversation.

### Extraction (free-tier LLM, rules first)
[ADR 0002](docs/adr/0002-llm-free-tier-rules-first.md). Deterministic pt-BR rules extract
age, vehicle, CEP, start date ("dia 15", "mês que vem", "01/11"), plan and intent. The LLM
(any OpenAI-compatible endpoint; Groq free tier by default) is called **only when the
rules find nothing**, receives **masked text**, and its output is validated like user
input. When the LLM is rate-limited or down, the agent falls back to the rules.

### When the `/quote` API fails
This is the core of the challenge. Policy in [ADR 0004](docs/adr/0004-resilience-policy.md).

| Situation | Behaviour |
|---|---|
| 5xx / connection error | Retry with exponential backoff + full jitter (max 4 attempts) |
| Slow call (the service sleeps 8 s) | Cut at 3 s and retry. Total budget 12 s per quote |
| First retry | Lead receives "Só um instante, o sistema de cotação está um pouco lento…" once |
| 429 + `Retry-After` | Honoured (capped) |
| 5 consecutive failures | Circuit opens for 30 s: later leads fail fast instead of piling on a dead legacy system. Half-open with a single probe |
| Everything exhausted | **No price.** Honest message + handoff `quote_unavailable` with the profile already collected |
| API comes back | Background worker re-quotes pending `quote_unavailable` tickets; if successful, the lead gets the quote automatically and the ticket closes as `auto_requoted` |
| HTTP 422 (business refusal) | Not retried. The reason is explained and the lead is handed off for alternatives |
| HTTP 400 / malformed 200 | Not retried. Logged as an error and handed off (`system_error`). Never shown as a price |

### Handoff criteria
Explicit and in one module: [`docs/handoff-criteria.md`](docs/handoff-criteria.md).

| Trigger | Reason |
|---|---|
| Lead asks for a person (any state; the LLM can't override it) | `customer_request` |
| Lead accepts a quote (issuance and payment are human work) | `plan_accepted` |
| Quote API unavailable after the resilience policy | `quote_unavailable` |
| Underwriting refusal (pre-screen or 422) | `underwriting_refusal` |
| 2nd price objection, or objection on the cheapest plan | `negotiation` |
| 3 turns without progress | `collection_stalled` |
| Claims, cancellation, complaints, other products | `out_of_scope` |
| Our request rejected / malformed response | `system_error` |

### Personal data (LGPD)
[ADR 0005](docs/adr/0005-data-minimisation.md). **The agent does not ask for CPF.** The
quote needs only age, vehicle, CEP and dates. Unsolicited CPF, e-mail, phone, plate and
known names are masked before the LLM, inside the logging pipeline and before
persistence. The phone and the CEP are Fernet-encrypted at rest, and lookups use an HMAC.
Tests prove that the scenario PII never reaches `execution.log`, and that 0 of the
dataset's lead messages keep raw PII after masking.

### Traceability
Each inbound message gets a `trace_id`, taken from `X-Trace-Id` or generated. It is
propagated via contextvars to every log line and forwarded to the quote service.
Persisted per conversation:
- `messages`: direction, masked body, state, `trace_id`, and a unique WhatsApp id for
  idempotency;
- `quotes`: id, status, masked request, response, and every attempt;
- `handoffs`: reason, detail, masked summary, status, resolution.

Key log events: `message_received`, `extraction_result`, `state_transition`,
`quote_requested`, `upstream_attempt`, `upstream_retry_scheduled`,
`circuit_state_changed`, `quote_succeeded|refused|unavailable`, `handoff_created`,
`handoff_auto_resolved`, `outbound_message`.

### Using the dataset
`make eval` replays all 2,500 conversations through the extractor and compares the
results with the dataset's labels:

| Field | Accuracy |
|---|---|
| Driver age (`lead_idade_informada`) | 100.0% |
| Vehicle year | 100.0% |
| Vehicle make/model (`veiculo_texto`) | 100.0% |
| CEP detected | 100.0% |
| Lead messages with raw PII after masking | 0 |

The evaluation found two real bugs, both fixed and covered by regression tests:
"Gol 2008" was parsed as a *Peugeot 2008*, and "pode mandar sim", the reply to 757
counter-offers, was not recognised. **Caveat:** the dataset is template-generated, so
these numbers measure coverage of its patterns, not real-world accuracy. That gap is why
the LLM fallback exists.

---

## Architecture

Hexagonal: the domain and the FSM are pure; ports are `Protocol`s; adapters are wired in
one composition root (`src/bootstrap.py`).

```
src/
├── domain/        models, validators, ports (QuoteService, UnitOfWork, OutboundChannel)
├── agent/         fsm.py, handoff_policy.py, extraction.py, prompts.py, responses.py (pt-BR copy),
│                  orchestrator.py (application service), requote_worker.py
├── llm/           port + OpenAI-compatible client (free tiers)
├── tools/         resilience.py (retry/jitter/breaker), http_client.py, quote_client.py
├── channels/      whatsapp.py (webhook models, signature, simulated + Cloud API senders)
├── infra/db/      SQLAlchemy models, repositories (UnitOfWork), Alembic migrations
├── api/           FastAPI app: webhook, health/ready, audit timeline, trace-id middleware
├── utils/         pii.py (masking), crypto.py (Fernet/HMAC), logging.py (JSON + trace_id)
├── simulation/    scenarios.yaml, runner, deterministic fault injection
├── config.py      typed settings
└── main.py        CLI: --serve | --run-simulation | --interactive
quote-service/     legacy quote API, vendored unchanged (only comments translated)
tests/             unit/ integration/ contract/ e2e/
scripts/           fetch_dataset, eval_extraction, export_ai_logs
docs/              handoff-criteria.md, adr/
logs/reference/    committed reference runs
ai-logs/           exported AI sessions (sanitized)
```

Only the customer-facing messages are Portuguese (`src/agent/responses.py`), because
the leads are Brazilian. Code, logs and docs are English. The legacy API's field names
(`plano_id`, `idade`…) are an external contract and stay as they are.

## Decisions log
| ADR | Decision |
|---|---|
| [0001](docs/adr/0001-own-state-machine.md) | Hand-written pure FSM instead of an agent framework |
| [0002](docs/adr/0002-llm-free-tier-rules-first.md) | Free-tier LLM behind an OpenAI-compatible port; rules first |
| [0003](docs/adr/0003-postgres-row-lock-idempotency.md) | PostgreSQL, one tx per message, row lock, idempotency |
| [0004](docs/adr/0004-resilience-policy.md) | Retry/timeout/breaker numbers for the legacy API |
| [0005](docs/adr/0005-data-minimisation.md) | Data minimisation, masking, encryption at rest |
| [0006](docs/adr/0006-async-webhook.md) | Fast webhook ack, processing after the response |

## Known limitations and next steps
- **Durable queue** between the webhook and the orchestrator (in-process background tasks
  are lost on a crash; idempotency already makes redelivery safe).
- **Outbox pattern** for outbound messages (today they are sent after commit; a send
  failure is logged, not retried).
- **Seller console / CRM integration** for handoff tickets (today: the `handoffs` table +
  `handoff_created` log event).
- **Metrics** (Prometheus/OpenTelemetry) on quote latency, breaker state and handoff rate.
  The structured logs already carry every field needed.
- **Audio transcription** for voice messages (now politely refused).
- The `WhatsAppCloudSender` follows Meta's API but was not exercised against a real
  number.
- Collection asks for all missing fields at once. A/B test against one question per turn.

## AI usage
This project was built with Claude Code. The sanitized sessions are in
[`ai-logs/`](ai-logs/) (`make ai-logs` regenerates them).
