"""Text normalisation helpers for pt-BR WhatsApp messages."""

from __future__ import annotations

import re
import unicodedata


def normalize(text: str) -> str:
    """Lower-case, strip accents and collapse whitespace ("Não  É" -> "nao e")."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped.lower()).strip()
