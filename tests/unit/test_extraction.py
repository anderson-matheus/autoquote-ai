from __future__ import annotations

from datetime import date

import pytest

from src.agent.extraction import (
    Extraction,
    ExtractionContext,
    HybridExtractor,
    Intent,
    RuleBasedExtractor,
    merge_extractions,
    parse_llm_extraction,
)
from src.domain.models import ConversationState, LeadField
from tests.fakes import FakeLLM

TODAY = date(2026, 10, 6)
S = ConversationState
rules = RuleBasedExtractor()


def ex(text: str, state: S = S.COLLECTING, expected: tuple[LeadField, ...] = ()) -> Extraction:
    return rules.extract_sync(text, ExtractionContext(state, TODAY, expected))


@pytest.mark.parametrize(
    ("text", "model", "year"),
    [
        ("Toyota Corolla 2008", "Toyota Corolla", 2008),
        ("e um Sandero 2022", "Renault Sandero", 2022),
        ("Onix Plus, ano 2019", "Chevrolet Onix Plus", 2019),
        ("Corolla Cross 2023", "Toyota Corolla Cross", 2023),
        ("Gol 14", "Volkswagen Gol", 2014),
        ("tenho um T-Cross 2021", "Volkswagen T-Cross", 2021),
        ("é um carro 2015", None, 2015),
    ],
)
def test_vehicle(text: str, model: str | None, year: int) -> None:
    result = ex(text)
    assert (result.vehicle_model, result.vehicle_year) == (model, year)
    assert result.intent is Intent.PROVIDE_INFO


@pytest.mark.parametrize(
    ("text", "age"),
    [
        ("Tenho 35 anos, cep 26703-384, cpf 389.083.863-43", 35),
        ("Cpf 123.456.789-09, tenho 82 anos, CEP 01310-100", 82),
        ("idade: 41", 41),
        ("30 anos de idade", 30),
        ("nasci em 1990", 36),
        ("o carro tem 5 anos", None),
    ],
)
def test_age(text: str, age: int | None) -> None:
    assert ex(text).age == age


def test_cpf_and_phone_digits_never_become_age_or_year() -> None:
    result = ex("cpf 20221990123 e whats +55 21 92019-2020")
    assert (result.age, result.vehicle_year, result.zip_code) == (None, None, None)


def test_bare_number_is_age_only_when_age_is_expected() -> None:
    assert ex("35", expected=(LeadField.AGE,)).age == 35
    assert ex("35").age is None


@pytest.mark.parametrize(
    ("text", "expected", "zip_code"),
    [
        ("cep 26703-384", (), "26703-384"),
        ("CEP: 01310100", (), "01310100"),
        ("01310100", (LeadField.ZIP_CODE,), "01310100"),
        ("cep 0131", (), "0131"),  # kept so validation can explain the problem
        ("01310100", (), "01310100"),
    ],
)
def test_zip(text: str, expected: tuple[LeadField, ...], zip_code: str) -> None:
    assert ex(text, expected=expected).zip_code == zip_code


@pytest.mark.parametrize(
    ("text", "start"),
    [
        ("hoje", date(2026, 10, 6)),
        ("amanhã", date(2026, 10, 7)),
        ("depois de amanha", date(2026, 10, 8)),
        ("dia 15", date(2026, 10, 15)),
        ("dia 2", date(2026, 11, 2)),
        ("01/11", date(2026, 11, 1)),
        ("15/07/2027", date(2027, 7, 15)),
        ("2026-12-01", date(2026, 12, 1)),
        ("mês que vem", date(2026, 11, 1)),
        ("31/02", None),
    ],
)
def test_start_date_when_expected(text: str, start: date | None) -> None:
    assert ex(text, expected=(LeadField.START_DATE,)).start_date == start


def test_start_date_needs_hint_when_not_expected() -> None:
    assert ex("nasci em 10/05/1990").start_date is None
    assert ex("quero que comece dia 20").start_date == date(2026, 10, 20)


@pytest.mark.parametrize(
    ("text", "state", "intent"),
    [
        ("quero falar com um atendente", S.COLLECTING, Intent.REQUEST_HUMAN),
        ("me liga por favor", S.PRESENTING, Intent.REQUEST_HUMAN),
        ("bati o carro ontem, como aciono?", S.COLLECTING, Intent.OUT_OF_SCOPE),
        ("quero cancelar meu seguro", S.COLLECTING, Intent.OUT_OF_SCOPE),
        ("fechado!", S.PRESENTING, Intent.ACCEPT),
        ("pode emitir entao", S.PRESENTING, Intent.ACCEPT),
        ("sim", S.PRESENTING, Intent.ACCEPT),
        ("sim", S.COLLECTING, Intent.OTHER),
        ("nao precisa, obrigado", S.PRESENTING, Intent.DECLINE),
        ("o preco ta salgado", S.PRESENTING, Intent.OBJECTION),
        ("a franquia ta alta", S.PRESENTING, Intent.OBJECTION),
        ("vou ver com minha esposa", S.PRESENTING, Intent.OBJECTION),
        ("o que cobre?", S.PRESENTING, Intent.ASK_DETAILS),
        ("Oi, queria fazer um seguro", S.NEW, Intent.GREETING),
    ],
)
def test_intents(text: str, state: S, intent: Intent) -> None:
    assert ex(text, state).intent is intent


@pytest.mark.parametrize(
    ("text", "state", "plan"),
    [
        ("quero o completo", S.PLAN_SELECTION, "completo"),
        ("Premium", S.PLAN_SELECTION, "premium"),
        ("o 2", S.PLAN_SELECTION, "completo"),
        ("o mais barato", S.PLAN_SELECTION, "essencial"),
        ("o mais barato", S.COLLECTING, None),
        ("meu nome completo é Ana", S.COLLECTING, None),
    ],
)
def test_plan(text: str, state: S, plan: str | None) -> None:
    assert ex(text, state).plan_id == plan


# ------------------------------------------------------------------ hybrid


def ctx(state: S = S.COLLECTING) -> ExtractionContext:
    return ExtractionContext(state, TODAY, (), known_names=("Ursula",))


async def test_llm_not_called_when_rules_are_conclusive() -> None:
    llm = FakeLLM({"intent": "objection"})
    result = await HybridExtractor(rules, llm).extract("Corolla 2022", ctx())
    assert llm.prompts == []
    assert result.source == "rules"


async def test_llm_fills_gaps_and_only_sees_masked_text() -> None:
    llm = FakeLLM({"vehicle_model": "Fiat Uno", "vehicle_year": 2010, "intent": "provide_info"})
    text = "Ursula aqui, cpf 389.083.863-43, meu carrinho é aquele uninho dez"
    result = await HybridExtractor(rules, llm).extract(text, ctx())
    _, user_prompt = llm.prompts[0]
    assert "389.083" not in user_prompt
    assert "Ursula" not in user_prompt
    assert (result.vehicle_model, result.vehicle_year, result.source) == (
        "Fiat Uno",
        2010,
        "rules+llm",
    )


async def test_llm_failure_falls_back_to_rules() -> None:
    result = await HybridExtractor(rules, FakeLLM(fail=True)).extract("hmm", ctx())
    assert (result.intent, result.source) == (Intent.OTHER, "rules")


def test_parse_llm_output_rejects_garbage() -> None:
    parsed = parse_llm_extraction(
        {
            "age": "abc",
            "vehicle_year": True,
            "plan_id": "gold",
            "start_date": "tomorrow",
            "intent": "dance",
            "vehicle_model": "  ",
        },
        ctx(),
    )
    assert parsed == Extraction(intent=Intent.OTHER, source="llm")
    ok = parse_llm_extraction(
        {"age": "42", "plan_id": "PREMIUM", "start_date": "2026-11-01", "intent": "accept"},
        ctx(),
    )
    assert (ok.age, ok.plan_id, ok.start_date, ok.intent) == (
        42,
        "premium",
        date(2026, 11, 1),
        Intent.ACCEPT,
    )


def test_merge_keeps_safety_intents_and_rule_values() -> None:
    rule = Extraction(intent=Intent.REQUEST_HUMAN, age=30, zip_code="01310-100")
    llm = Extraction(intent=Intent.ACCEPT, age=31, vehicle_year=2020, zip_code="99999-999")
    merged = merge_extractions(rule, llm)
    assert (merged.intent, merged.age, merged.vehicle_year, merged.zip_code) == (
        Intent.REQUEST_HUMAN,
        30,
        2020,
        "01310-100",
    )
    other = merge_extractions(Extraction(), Extraction(intent=Intent.OTHER, age=40))
    assert other.intent is Intent.PROVIDE_INFO


def test_numeric_model_names_need_the_make() -> None:
    assert ex("Gol 2008").vehicle_model == "Volkswagen Gol"
    assert ex("peugeot 2008 ano 2020").vehicle_model == "Peugeot 2008"


def test_dataset_counter_offer_reply_is_affirmative() -> None:
    assert ex("pode mandar sim", S.PRESENTING).intent is Intent.ACCEPT
