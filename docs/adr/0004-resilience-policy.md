# ADR 0004 — Resilience policy for the legacy `/quote`

**Status:** accepted

## Context
`/quote` fails 20% of the time (500/502/503) and sleeps 8 s in 10% of the calls. A
WhatsApp lead waits for an answer, and wrong prices are unacceptable.

## Decision
| Knob | Value | Rationale |
|---|---|---|
| Per-attempt timeout | 3 s | A healthy call takes milliseconds. 8 s is the failure mode, so cut it early. |
| Total deadline per quote | 12 s | A bounded, chat-acceptable wait. With 4 attempts the chance that all of them fail by chance is about 0.3⁴ ≈ 0.8%. |
| Max attempts | 4 | |
| Backoff | exponential, base 0.4 s, cap 3 s, **full jitter** | Avoids synchronized retry storms across conversations. |
| Retry on | timeout, connection error, 5xx, 429 (honours `Retry-After`) | Transient failures only. |
| Never retry | 400, 422 | They are deterministic. 422 is a business refusal and leads to a handoff. 400 is our bug and leads to a handoff plus an error log. |
| Circuit breaker | 5 consecutive failures → open 30 s → half-open single probe | Stops hammering a legacy system that is down and fails fast for the next leads. |
| UX | first failed attempt → "Só um instante…" sent once | The lead knows the agent did not freeze. |
| Exhausted | handoff `quote_unavailable` + background re-quote worker | No invented price, and a recovered API does not cost the sale. |

The catalog (`/planos`) has its own breaker and a TTL cache with **stale-while-error**.
Plan names and coverages rarely change, and prices are never cached.

## Consequences
All timing goes through an injected `Clock`, so the tests are deterministic and never
sleep. Every attempt is logged (`upstream_attempt`) and persisted (`quotes.attempts`).
