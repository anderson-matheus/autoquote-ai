# AutoQuote AI — Documentation

Technical documentation for the AutoQuote AI agent. The [project README](../README.md)
is the quick start; these pages explain **how the system is built and why**.

| Page | What you will find |
|---|---|
| [Architecture](architecture.md) | System context, containers, layers, module map, dependency rules, runtime flows (sequence diagrams) |
| [Conversation flow](conversation-flow.md) | State machine, extraction pipeline (rules + LLM), intents, validation, reply rendering |
| [Handoff criteria](handoff-criteria.md) | When and why the agent hands the conversation to a human seller |
| [Resilience](resilience.md) | Failure taxonomy of the legacy quote API, timeouts, retries, circuit breaker, re-quote worker |
| [Data model & privacy](data-model-and-privacy.md) | Database schema (ERD), what is stored and how, PII masking/encryption, LGPD posture |
| [Observability](observability.md) | Trace ids, structured log event catalog, audit API, how to investigate a conversation |
| [Testing & quality](testing-and-quality.md) | Test strategy, suites, determinism, quality gates, CI pipeline, branch policy |
| [Operations](operations.md) | Running, full configuration reference, production checklist, migrations, troubleshooting, scaling |
| [Development guide](development.md) | Local setup, conventions, and step-by-step recipes to extend the agent |
| [Architecture Decision Records](adr/README.md) | The decisions behind the design, with context and consequences |

## Reading paths

- **Evaluating the challenge?** Read [Architecture](architecture.md), then
  [Resilience](resilience.md) and [Handoff criteria](handoff-criteria.md). The
  reference runs are in [`logs/reference/`](../logs/reference/).
- **Operating it?** Read [Operations](operations.md) and [Observability](observability.md).
- **Changing it?** Read the [Development guide](development.md) and
  [Testing & quality](testing-and-quality.md).

## Glossary

| Term | Meaning |
|---|---|
| Lead | A prospective customer talking to the agent on WhatsApp |
| Seller / human | AutoSeguro sales staff who receive handoffs |
| Legacy quote service | The vendored `quote-service/` (`/planos`, `/quote`), unreliable by design |
| Quote | A price **returned by the legacy service**. The agent never computes one |
| Handoff | Transfer of the conversation to a human, with a ticket in the `handoffs` table |
| Turn | Processing of one inbound message, from webhook to replies |
| Snapshot | The persisted conversation state the state machine works on |
| Effect | A side effect requested by the state machine (request a quote, open a handoff) |
| Breaker | Circuit breaker protecting an upstream endpoint |
| PII | Personal data (CPF, e-mail, phone, CEP, plate, names) |
