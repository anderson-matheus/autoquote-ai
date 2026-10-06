from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.domain import validators
from src.domain.errors import InvalidFieldError

TODAY = date(2026, 10, 6)


@pytest.mark.parametrize("age", [16, 35, 110])
def test_valid_age(age: int) -> None:
    assert validators.validate_age(age) == age


@pytest.mark.parametrize("age", [0, 15, 111, 300])
def test_invalid_age(age: int) -> None:
    with pytest.raises(InvalidFieldError) as exc:
        validators.validate_age(age)
    assert exc.value.field == "age"


def test_vehicle_year_bounds() -> None:
    assert validators.validate_vehicle_year(2027, TODAY) == 2027
    for bad in (1949, 2028):
        with pytest.raises(InvalidFieldError):
            validators.validate_vehicle_year(bad, TODAY)


@pytest.mark.parametrize("raw", ["01310-100", "01310100", " 01.310-100 "])
def test_zip_normalized(raw: str) -> None:
    assert validators.normalize_zip_code(raw) == "01310-100"


@pytest.mark.parametrize("raw", ["0131", "013101000", "00000-000", "abc"])
def test_zip_invalid(raw: str) -> None:
    with pytest.raises(InvalidFieldError):
        validators.normalize_zip_code(raw)


def test_start_date_window() -> None:
    assert validators.validate_start_date(TODAY, TODAY, 90) == TODAY
    with pytest.raises(InvalidFieldError, match="passado"):
        validators.validate_start_date(TODAY - timedelta(days=1), TODAY, 90)
    with pytest.raises(InvalidFieldError, match="90 dias"):
        validators.validate_start_date(TODAY + timedelta(days=91), TODAY, 90)
