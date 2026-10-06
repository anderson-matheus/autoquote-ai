# Architecture Decision Records

Short documents capturing significant decisions: the context, what was decided and the
consequences, in the style of [Michael Nygard](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions).
A new significant decision gets the next number. A superseded ADR is kept, and its status
is changed to point to the one that replaces it.

| # | Decision | Status |
|---|---|---|
| [0001](0001-own-state-machine.md) | Hand-written pure state machine instead of an agent framework | Accepted |
| [0002](0002-llm-free-tier-rules-first.md) | Free-tier LLM behind an OpenAI-compatible port; rules first | Accepted |
| [0003](0003-postgres-row-lock-idempotency.md) | PostgreSQL, one transaction per message, row lock, idempotency | Accepted |
| [0004](0004-resilience-policy.md) | Timeouts, retries, jitter and circuit breaker for the legacy API | Accepted |
| [0005](0005-data-minimisation.md) | Data minimisation, masking at every boundary, encryption at rest | Accepted |
| [0006](0006-async-webhook.md) | Acknowledge the webhook immediately, process after the response | Accepted |
| [0007](0007-quality-gates-and-architecture-enforcement.md) | Automated quality gates and enforced architecture boundaries | Accepted |
| [0008](0008-vendor-legacy-service-unchanged.md) | Vendor the legacy quote service unchanged, test it as a contract | Accepted |

Template:

```markdown
# ADR NNNN — Title

**Status:** proposed | accepted | superseded by NNNN

## Context
## Decision
## Consequences
```
