# ADR 0007 — Automated quality gates and enforced architecture boundaries

**Status:** accepted

## Context
The project is meant to be maintained by other engineers, and it is built largely with an
AI coding assistant. Conventions that are only written down drift: a domain module
imports SQLAlchemy "just this once", a dependency with a known CVE slips in, a test
starts depending on wall-clock time. Review alone does not catch these reliably.

## Decision
Every rule that can be checked by a machine is checked in CI, and `main` only accepts
changes that pass all checks:

- **Style and bugs:** ruff (including the bandit `S`, pylint `PL` and bugbear `B` rule
  sets) and ruff format.
- **Types:** mypy `--strict` on all application code and scripts.
- **Architecture:** import-linter contracts encode the hexagonal boundaries (pure domain;
  application depends only on ports; adapters never import the application; only the
  composition root wires adapters). A probe violation breaks the build.
- **Tests:** unit, integration on real PostgreSQL, contract and E2E. Branch coverage
  ≥ 85% and warnings treated as errors.
- **Security:** bandit, pip-audit, gitleaks, and Trivy on the production image and the
  Dockerfiles.
- **Schema:** `alembic check` (migrations match models).
- **System:** a Docker stack smoke test with a fail-closed PII check on the real log.
- **Process:** branch protection on `main` (PR required, all checks required, up to date,
  linear history, no force push, admins included). Dependabot for dependencies, base
  images and actions.

## Consequences
- The rules are executable documentation. A newcomer learns them from failing checks,
  not from tribal knowledge.
- CI takes about 2–3 minutes per PR, an acceptable cost.
- Gates found real problems during development: name-masking order, SQLite SAVEPOINT
  semantics, leaked file handles, a deprecated constant, a fail-open PII check in CI and
  a flaky outage scenario.
- Direct pushes to `main` are no longer possible, even for admins. Emergency fixes go
  through a PR too.
