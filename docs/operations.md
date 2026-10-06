# Operations

## 1. Running

### Docker (recommended)

```bash
cp .env.example .env
make up                       # docker compose up --build -d, with AGENT_UID/GID for logs/
docker compose ps             # agent, quote-service, postgres — all healthy
curl localhost:8080/ready
```

| Command | Purpose |
|---|---|
| `make up` / `make down` | Start / stop the stack |
| `make logs` | Follow container logs |
| `make simulate` | Run the scripted scenarios inside the stack |
| `docker compose exec agent python -m src.main --interactive` | Chat with the agent |
| `docker compose exec agent pytest tests/` | Full test suite in the container (uses `autoquote_test` DB) |

### Without Docker

```bash
make install                  # uv sync --all-groups + pre-commit hooks
# start the legacy service
(cd quote-service && uv run uvicorn app.main:app --port 8000) &
make simulate-local           # scenarios on a throwaway SQLite DB
uv run python -m src.main --interactive --sqlite
```

The HTTP API needs PostgreSQL: `DATABASE_URL=… uv run alembic upgrade head && uv run python -m src.main --serve`.

### Entry points

| Command | What it does |
|---|---|
| `python -m src.main --serve [--host --port]` | HTTP API (webhook, health, audit) |
| `python -m src.main --run-simulation [--scenario NAME] [--transcript-out FILE] [--sqlite]` | Scripted conversations; exit code ≠ 0 if any fails |
| `python -m src.main --interactive [--sqlite]` | Terminal chat |
| `uvicorn --factory src.api.app:app_factory` | Production entry point (used by the image) |

## 2. HTTP API

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /webhooks/whatsapp` | `hub.verify_token` | Meta verification handshake (echoes `hub.challenge`) |
| `POST /webhooks/whatsapp` | `X-Hub-Signature-256` when `WHATSAPP_APP_SECRET` is set | Receive messages. `?sync=true` (non-production) returns replies inline |
| `GET /health` | — | Liveness |
| `GET /ready` | — | DB check, breaker states, catalog cache |
| `GET /conversations/{id}` | `X-Api-Key` when `ADMIN_API_KEY` is set (disabled in production without it) | Audit timeline |
| `GET /docs` | — | OpenAPI UI |

## 3. Configuration reference

All settings are environment variables (or `.env`). Empty values mean "not set".

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `development` | `development`, `test` or `production`. Production requires `PII_ENCRYPTION_KEY` and disables `?sync=true` |
| `LOG_LEVEL` | `INFO` | Root log level |
| `LOG_FILE` | `logs/execution.log` | JSON log file (rotating). Empty = stdout only |
| `DATABASE_URL` | `postgresql+asyncpg://autoquote:autoquote@localhost:5432/autoquote` | SQLAlchemy async URL |
| `QUOTE_SERVICE_URL` | `http://localhost:8000` | Legacy quote service base URL (`http://quote-service:8000` in compose) |
| `QUOTE_ATTEMPT_TIMEOUT_S` | `3.0` | Per-attempt timeout |
| `QUOTE_TOTAL_DEADLINE_S` | `12.0` | Total budget per quote |
| `QUOTE_MAX_ATTEMPTS` | `4` | Max attempts per quote |
| `QUOTE_BACKOFF_BASE_S` / `QUOTE_BACKOFF_MAX_S` | `0.4` / `3.0` | Exponential backoff (full jitter) |
| `BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive failures to open the breaker |
| `BREAKER_RECOVERY_TIMEOUT_S` | `30.0` | Open → half-open delay |
| `CATALOG_CACHE_TTL_S` | `300.0` | `/planos` cache TTL (stale-while-error) |
| `LLM_API_KEY` | — | OpenAI-compatible key. Empty = rules only, fully offline |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | Any OpenAI-compatible endpoint (Groq, OpenRouter, Gemini, Ollama) |
| `LLM_MODEL` | `llama-3.3-70b-versatile` | Model name |
| `LLM_TIMEOUT_S` | `8.0` | LLM request timeout |
| `MAX_STALLED_TURNS` | `3` | Turns without progress before handoff |
| `MAX_START_DATE_DAYS_AHEAD` | `90` | Latest accepted coverage start |
| `REQUOTE_WORKER_ENABLED` | `true` | Background re-quote after outages |
| `REQUOTE_INTERVAL_S` | `60.0` | Worker cycle |
| `REQUOTE_MAX_AGE_S` | `1800.0` | Only handoffs younger than this are retried |
| `PII_ENCRYPTION_KEY` | — | Fernet key for phone and CEP at rest. **Required in production** |
| `CONTACT_HASH_SECRET` | dev value | HMAC secret for contact lookup. **Set a strong value in production** |
| `ADMIN_API_KEY` | — | Protects the audit API |
| `WHATSAPP_VERIFY_TOKEN` | `dev-verify-token` | Webhook verification token |
| `WHATSAPP_APP_SECRET` | — | Enables signature verification |
| `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` | — | When both are set, replies go through the Graph API instead of being simulated |
| `WHATSAPP_API_BASE_URL` | `https://graph.facebook.com/v20.0` | Graph API base |

Compose-only variables: `QUOTE_SERVICE_PORT` / `AGENT_PORT` (host ports), `AGENT_UID` /
`AGENT_GID` (bind-mount ownership), `DOCKER_BUILD_NETWORK` (build network),
`QUOTE_FAILURE_RATE` / `QUOTE_SLOW_RATE` / `QUOTE_SLOW_SECONDS` / `QUOTE_SEED` (legacy chaos).

## 4. Production checklist

- [ ] `ENVIRONMENT=production`
- [ ] `PII_ENCRYPTION_KEY` generated (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) and stored in a secret manager
- [ ] Strong `CONTACT_HASH_SECRET` and `ADMIN_API_KEY`
- [ ] `WHATSAPP_APP_SECRET` set (signature verification on), plus `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`
- [ ] Build the `runtime` image target (no dev deps, no tests, non-root)
- [ ] Managed PostgreSQL. Migrations run on start (`alembic upgrade head`); with several replicas, run them as a release step instead
- [ ] Ship stdout logs to the log platform; alert on `handoff_created` rate, `circuit_state_changed` to open, and `message_processing_failed`
- [ ] Readiness probe `/ready`, liveness `/health`

## 5. Database migrations

```bash
uv run alembic revision --autogenerate -m "describe change"   # after changing src/infra/db/models.py
uv run alembic upgrade head
make migrations-check                                         # CI does this too
```

Review autogenerated migrations by hand. Keep them backwards compatible (expand, then
contract) when running several replicas.

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `port is already allocated` | 8000/8080 in use | `QUOTE_SERVICE_PORT=28000 AGENT_PORT=28080 make up` |
| Build fails: `Temporary failure in name resolution` | Build containers cannot reach the DNS (e.g. IPv6-only resolvers) | `DOCKER_BUILD_NETWORK=host make up` |
| `log_file_unavailable` / `Permission denied: logs/execution.log` | The bind-mounted `logs/` is not writable by the container user | Use `make up` (exports `AGENT_UID`/`AGENT_GID`) |
| Every quote ends in `quote_unavailable` | Legacy service down, or chaos turned up | `curl :8000/health`; check `QUOTE_FAILURE_RATE`; `/ready` shows breaker states |
| `PII_ENCRYPTION_KEY is required in production` | Production without a key | Set the key (see checklist) |
| `cannot decrypt field` | Key changed and old rows can't be read | Restore the old key; plan rotation with `MultiFernet` |
| LLM not used (`extractor_configured` shows `llm: null`) | `LLM_API_KEY` empty | Expected: rules only. Set a key to enable |
| `llm_fallback_to_rules` with `http 429` | Free-tier rate limit | Expected and harmless; the LLM breaker backs off for 60 s |

## 7. Scaling and next steps

| Concern | Today | Next step |
|---|---|---|
| Webhook processing | In-process background task; idempotent | Durable queue (Redis Streams, SQS or Pub/Sub) between the webhook and workers |
| Outbound messages | Sent after commit; failures logged | Transactional outbox with a sender worker and retries |
| Concurrency per lead | Row lock held during the quote call (≤ 12 s) | Release the lock during upstream calls (optimistic `version` check) if per-lead concurrency grows |
| Breaker state | Per process | Shared state (e.g. Redis) when running many replicas |
| Seller integration | `handoffs` table + log event | CRM / helpdesk integration with ticket assignment |
| Metrics | Structured logs | Prometheus / OpenTelemetry |
| Data retention | Not implemented | Retention policy and purge job; data-subject requests |
| Voice messages | Politely refused | Speech-to-text, then the same pipeline |
