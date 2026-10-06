# ADR 0005 — Data minimisation and PII handling (LGPD)

**Status:** accepted

## Context
Leads paste CPF, e-mail, phone and plate into the chat (the dataset is full of them), and
the challenge explicitly evaluates how carefully we treat this data.

## Decision
- **Collect only what the quote needs**: age, vehicle year/model, CEP, start date, plan.
  The agent **never asks for CPF**. Unsolicited CPF, e-mail, phone or plate values are
  masked and never stored in clear text.
- Masking (`src/utils/pii.py`) runs at every boundary:
  - before LLM calls;
  - as a mandatory processor in the structured-log pipeline, so even a careless
    `log.info(body=raw)` is masked;
  - before persisting message bodies.
- At rest: the contact address (phone) and the CEP are **Fernet-encrypted**. Conversations
  are looked up by an **HMAC** of the phone. The quote payload is stored with the CEP
  masked to its 5-digit prefix. The re-quote fingerprint is a SHA-256 digest.
- Production refuses to start without `PII_ENCRYPTION_KEY`. Development derives a key
  from a passphrase and logs a warning.
- The dataset is never committed (`dataset/` is git-ignored and fetched by a script). The
  AI session export is sanitized by `scripts/export_ai_logs.py`.

## Consequences
The E2E suite asserts that the execution log contains none of the scenario PII, and the
dataset evaluation asserts that 0 lead messages keep unmasked PII after masking. The
trade-off is that a seller who needs the CPF to issue the policy collects it in the human
part of the flow.
