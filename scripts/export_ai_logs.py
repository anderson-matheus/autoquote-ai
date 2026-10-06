"""Exports Claude Code sessions for this project into ai-logs/, sanitized.

The challenge requires publishing the AI conversations. Before they go to a public repo:
* secrets are redacted (API keys, tokens, private keys, bearer headers);
* personal data in any string (CPF, e-mail, phone, CEP, plate) goes through the same
  masking used by the agent itself.
Only JSON string values are rewritten, so every line stays valid JSON.

    uv run python -m scripts.export_ai_logs [--source ~/.claude/projects/<slug>]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from src.utils.pii import mask_text

_SECRETS = re.compile(
    r"(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AIza[0-9A-Za-z_-]{30,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)"
)
# Partial addresses ("user@company", no TLD) slip past e-mail masking, e.g. inside a grep
# pattern typed during the session. Full addresses are handled by mask_text.
_PARTIAL_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z][\w-]*(?![\w.-]*\.[A-Za-z]{2,})")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}")
_ASSIGNED = re.compile(
    r"(?i)\b((?:LLM_API_KEY|PII_ENCRYPTION_KEY|ADMIN_API_KEY|WHATSAPP_ACCESS_TOKEN|"
    r"WHATSAPP_APP_SECRET|CONTACT_HASH_SECRET)\s*=\s*)(?!\s|$|change-me)[^\s\"']+"
)


def sanitize_text(text: str) -> str:
    text = _SECRETS.sub("[SECRET]", text)
    text = _BEARER.sub(r"\1[SECRET]", text)
    text = _ASSIGNED.sub(r"\1[SECRET]", text)
    text = mask_text(text)
    return _PARTIAL_EMAIL.sub("[EMAIL]", text)


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    return value


def export(source: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for session in sorted(source.glob("*.jsonl")):
        out = dest / session.name
        with session.open(encoding="utf-8") as src, out.open("w", encoding="utf-8") as dst:
            for line in src:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    dst.write(json.dumps({"unparseable_line": sanitize_text(line)}) + "\n")
                    continue
                dst.write(json.dumps(sanitize(record), ensure_ascii=False) + "\n")
        written.append(out)
    return written


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    default_source = Path.home() / ".claude" / "projects" / str(repo).replace("/", "-")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_source)
    parser.add_argument("--dest", type=Path, default=repo / "ai-logs" / "claude-code")
    args = parser.parse_args()
    if not args.source.exists():
        raise SystemExit(f"no sessions found at {args.source}")
    for path in export(args.source, args.dest):
        print(f"exported {path.relative_to(repo)} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
