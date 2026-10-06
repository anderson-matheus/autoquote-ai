"""Detection and masking of personal data (LGPD) in free text and structured log events.

Masking is applied as defence in depth at every boundary that leaves the process:
before LLM calls, inside the structured-log pipeline and before message persistence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PiiKind(StrEnum):
    EMAIL = "EMAIL"
    CPF = "CPF"
    CNPJ = "CNPJ"
    PHONE = "PHONE"
    ZIP = "CEP"
    PLATE = "PLACA"
    NAME = "NAME"


@dataclass(frozen=True, slots=True)
class PiiMatch:
    kind: PiiKind
    start: int
    end: int
    value: str


# Order matters: earlier patterns win on overlapping spans (CPF before phone, etc.).
_PATTERNS: tuple[tuple[PiiKind, re.Pattern[str]], ...] = (
    (PiiKind.EMAIL, re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)),
    (PiiKind.CNPJ, re.compile(r"(?<!\w)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\w)")),
    (PiiKind.CPF, re.compile(r"(?<!\w)\d{3}\.\d{3}\.\d{3}-?\d{2}(?!\w)|(?<!\w)\d{11}(?!\w)")),
    (
        PiiKind.PHONE,
        re.compile(
            r"(?<![\d\w])(?:\+?55[\s.-]?)?\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}(?!\d)",
        ),
    ),
    (PiiKind.ZIP, re.compile(r"(?<!\w)\d{5}-?\d{3}(?!\w)")),
    (
        PiiKind.PLATE,
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{3}-?\d[A-Za-z0-9]\d{2}(?![A-Za-z0-9])"),
    ),
)

# Structured-log keys whose values are always sensitive, whatever their format.
SENSITIVE_KEYS = frozenset(
    {
        "cpf",
        "email",
        "phone",
        "telefone",
        "wa_id",
        "contact",
        "contact_address",
        "plate",
        "placa",
        "name",
        "sender_name",
        "first_name",
        "nome",
        "authorization",
        "api_key",
        "llm_api_key",
        "access_token",
        "password",
        "secret",
    }
)
ZIP_KEYS = frozenset({"cep", "zip_code"})


def find_pii(text: str, known_names: Iterable[str] = ()) -> list[PiiMatch]:
    """Returns non-overlapping PII spans, ordered by position."""
    taken: list[PiiMatch] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < m.end and m.start < end for m in taken)

    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            if not overlaps(m.start(), m.end()):
                taken.append(PiiMatch(kind, m.start(), m.end(), m.group()))
    names = {n.strip() for n in known_names if n and len(n.strip()) >= 2}
    for name in sorted(names, key=len, reverse=True):  # "Ana Souza" before "Ana"
        for m in re.finditer(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            if not overlaps(m.start(), m.end()):
                taken.append(PiiMatch(PiiKind.NAME, m.start(), m.end(), m.group()))
    return sorted(taken, key=lambda m: m.start)


def mask_zip(value: str) -> str:
    """Keeps the 5-digit region prefix (useful for analytics), hides the rest."""
    digits = re.sub(r"\D", "", value)
    return f"{digits[:5]}-***" if len(digits) >= 5 else "[CEP]"


def mask_text(text: str, known_names: Iterable[str] = ()) -> str:
    matches = find_pii(text, known_names)
    if not matches:
        return text
    out: list[str] = []
    cursor = 0
    for m in matches:
        out.append(text[cursor : m.start])
        out.append(mask_zip(m.value) if m.kind is PiiKind.ZIP else f"[{m.kind.value}]")
        cursor = m.end
    out.append(text[cursor:])
    return "".join(out)


def mask_value(value: Any, key: str | None = None) -> Any:
    """Recursively masks a structured value (used by the logging pipeline)."""
    lowered = key.lower() if key else None
    if lowered in SENSITIVE_KEYS and value is not None:
        return "[REDACTED]"
    if lowered in ZIP_KEYS and isinstance(value, str):
        return mask_zip(value)
    if lowered is not None and lowered.endswith("_id") and isinstance(value, str):
        return value  # opaque identifiers (uuids, hashes) are not personal data
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping):
        return {k: mask_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [mask_value(v) for v in value]
    return value
