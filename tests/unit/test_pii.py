from __future__ import annotations

import itertools
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.utils.pii import PiiKind, find_pii, mask_text, mask_value, mask_zip


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cpf 389.083.863-43", "cpf [CPF]"),
        ("cpf 38908386343", "cpf [CPF]"),
        ("email ursula.souza@gmail.com ok", "email [EMAIL] ok"),
        ("whats +55 21 97224-2584", "whats [PHONE]"),
        ("(11) 98765-4321", "[PHONE]"),
        ("cep 26703-384", "cep 26703-***"),
        ("a placa é ABC1D23 se precisar", "a placa é [PLACA] se precisar"),
        ("placa abc-1234", "placa [PLACA]"),
        ("cnpj 12.345.678/0001-90", "cnpj [CNPJ]"),
        ("Toyota Corolla 2008, tenho 35 anos", "Toyota Corolla 2008, tenho 35 anos"),
    ],
)
def test_mask_text(text: str, expected: str) -> None:
    assert mask_text(text) == expected


def test_mask_known_names_case_insensitive() -> None:
    assert (
        mask_text("oi, aqui é a URSULA souza", ["Ursula Souza", "Ursula"]) == "oi, aqui é a [NAME]"
    )


def test_find_pii_spans_do_not_overlap() -> None:
    matches = find_pii("cpf 389.083.863-43 tel 21972242584 cep 01310-100")
    kinds = [m.kind for m in matches]
    assert kinds == [PiiKind.CPF, PiiKind.CPF, PiiKind.ZIP]
    for a, b in itertools.pairwise(matches):
        assert a.end <= b.start


def _cpf(rng: random.Random) -> str:
    n = [rng.randint(0, 9) for _ in range(9)]
    for _ in range(2):
        s = sum((len(n) + 1 - i) * v for i, v in enumerate(n))
        n.append((s * 10) % 11 % 10)
    d = "".join(map(str, n))
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


@given(seed=st.integers(), prefix=st.sampled_from(["CPF ", "cpf: ", "meu cpf é ", ""]))
def test_any_formatted_cpf_is_masked(seed: int, prefix: str) -> None:
    cpf = _cpf(random.Random(seed))
    masked = mask_text(f"{prefix}{cpf}, tenho 40 anos")
    assert cpf not in masked
    assert "[CPF]" in masked


@given(
    user=st.from_regex(r"[a-z]{1,10}[._]?[a-z0-9]{0,5}", fullmatch=True),
    domain=st.sampled_from(["gmail.com", "hotmail.com", "yahoo.com.br", "bol.com.br"]),
)
def test_any_email_is_masked(user: str, domain: str) -> None:
    assert "@" not in mask_text(f"meu email é {user}@{domain} beleza")


def test_mask_zip() -> None:
    assert mask_zip("01310-100") == "01310-***"
    assert mask_zip("12") == "[CEP]"


def test_mask_value_structured() -> None:
    event = {
        "body": "cpf 389.083.863-43",
        "cep": "01310-100",
        "sender_name": "Ursula",
        "conversation_id": "38908386343",  # ids are opaque, never rewritten
        "nested": {"phone": "123", "list": ["x@y.com", 3]},
    }
    assert mask_value(event) == {
        "body": "cpf [CPF]",
        "cep": "01310-***",
        "sender_name": "[REDACTED]",
        "conversation_id": "38908386343",
        "nested": {"phone": "[REDACTED]", "list": ["[EMAIL]", 3]},
    }
