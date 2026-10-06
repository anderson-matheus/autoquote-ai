# ADR 0008 — Vendor the legacy quote service unchanged, test it as a contract

**Status:** accepted

## Context
The challenge provides the quote API (`quote-service/`) as a stand-in for a real legacy
system: unreliable, Portuguese field names, business rules hidden in a JSON file. It is
tempting to "fix" it: remove the chaos, rename fields, add idempotency keys.

## Decision
- Vendor it into this repository **byte-for-byte in behaviour and contract**. Only
  comments and docstrings were translated to English, and the Dockerfile gained a
  non-root user and a healthcheck.
- Treat it as an external dependency: all knowledge of its contract lives in one adapter
  (`src/tools/quote_client.py`), which translates to domain types.
- Pin the contract with **consumer-driven contract tests** (`tests/contract/`). They run
  the real app in-process through our own parsers, so any drift (field rename, a new
  status code, a change in rounding) fails the build instead of production.
- Use its built-in chaos (`QUOTE_FAILURE_RATE`, `QUOTE_SLOW_RATE`) for demos, and
  deterministic fault injection (`FaultInjectingTransport`) for tests.

## Consequences
- The agent is proven against the system as it is, not as we would like it to be.
- The legacy service's own quirks (float rounding of premiums, e.g. 241.38 instead of
  241.39) are documented by the tests rather than hidden.
- Upgrading to a new version of the legacy API means re-vendoring and letting the contract
  tests show what changed.
- Dependabot does not update `quote-service/`. Its dependencies change only when it is
  re-vendored.
