# AI usage logs

Sessions with AI tools used to build this project, as required by the challenge.

- `claude-code/` — Claude Code sessions (`.jsonl`, one JSON event per line), exported with
  `make ai-logs` (`scripts/export_ai_logs.py`). Before export, API keys, tokens, bearer
  headers and personal data (CPF, e-mail, phone, CEP, plate) are redacted. Only JSON
  string values are rewritten, so the files stay valid JSON Lines.

How the AI was used: architecture planning (plan mode, then implementation), code and test
generation, and running the quality gates and the dockerised stack. Every decision is
documented in `docs/adr/`.
