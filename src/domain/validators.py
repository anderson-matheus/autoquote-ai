"""Validation/normalisation of lead-provided values. Pure functions."""

from __future__ import annotations

import re
from datetime import date, timedelta

from src.domain.errors import InvalidFieldError

MIN_VEHICLE_YEAR = 1950
MIN_AGE, MAX_AGE = 16, 110
_ZIP_RE = re.compile(r"^\d{8}$")


def validate_age(value: int) -> int:
    if not MIN_AGE <= value <= MAX_AGE:
        raise InvalidFieldError("age", f"idade deve estar entre {MIN_AGE} e {MAX_AGE}")
    return value


def validate_vehicle_year(value: int, today: date) -> int:
    if not MIN_VEHICLE_YEAR <= value <= today.year + 1:
        raise InvalidFieldError("vehicle_year", "ano do veículo inválido")
    return value


def normalize_zip_code(value: str) -> str:
    """Returns the canonical `NNNNN-NNN` form."""
    digits = re.sub(r"\D", "", value)
    if not _ZIP_RE.match(digits) or digits == "0" * 8:
        raise InvalidFieldError("zip_code", "CEP deve ter 8 dígitos")
    return f"{digits[:5]}-{digits[5:]}"


def validate_start_date(value: date, today: date, max_days_ahead: int) -> date:
    if value < today:
        raise InvalidFieldError("start_date", "a data de início não pode estar no passado")
    if value > today + timedelta(days=max_days_ahead):
        raise InvalidFieldError(
            "start_date", f"a data de início deve ser em até {max_days_ahead} dias"
        )
    return value
