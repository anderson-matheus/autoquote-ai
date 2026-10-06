"""Turns a raw lead message into structured data + intent.

Two extractors behind one interface:

* `RuleBasedExtractor` - deterministic regex/keyword rules for pt-BR WhatsApp text.
  Runs locally on the *raw* text, so CEP never has to leave the process.
* `HybridExtractor` - rules first; the LLM is called only when the rules are
  inconclusive (no field found and no clear intent). It receives *masked* text and
  its output is validated exactly like user input. Any LLM failure (free-tier rate
  limit, timeout, bad JSON) silently degrades to the rule-based result.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from src.agent.prompts import EXTRACTION_PROMPT_VERSION, build_extraction_prompt
from src.agent.text import normalize
from src.agent.vehicles import MAKE_ALIASES, MODELS_BY_MAKE
from src.domain.models import ConversationState, LeadField
from src.llm.port import LLMClient, LLMUnavailableError
from src.utils.logging import get_logger
from src.utils.pii import PiiKind, find_pii, mask_text

log = get_logger(__name__)


class Intent(StrEnum):
    PROVIDE_INFO = "provide_info"
    GREETING = "greeting"
    REQUEST_HUMAN = "request_human"
    ACCEPT = "accept"
    DECLINE = "decline"
    OBJECTION = "objection"
    ASK_DETAILS = "ask_details"
    OUT_OF_SCOPE = "out_of_scope"
    OTHER = "other"


# Intents that must never be overridden by the LLM: they trigger a human handoff and
# a false negative there is worse than an extra handoff.
_SAFETY_INTENTS = frozenset({Intent.REQUEST_HUMAN, Intent.OUT_OF_SCOPE})


@dataclass(frozen=True, slots=True)
class ExtractionContext:
    state: ConversationState
    today: date
    expected_fields: tuple[LeadField, ...] = ()
    plan_ids: tuple[str, ...] = ("essencial", "completo", "premium")
    known_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Extraction:
    intent: Intent = Intent.OTHER
    age: int | None = None
    vehicle_model: str | None = None
    vehicle_year: int | None = None
    zip_code: str | None = None
    start_date: date | None = None
    plan_id: str | None = None
    source: str = "rules"

    def has_fields(self) -> bool:
        return any(
            v is not None
            for v in (
                self.age,
                self.vehicle_model,
                self.vehicle_year,
                self.zip_code,
                self.start_date,
                self.plan_id,
            )
        )


class Extractor:
    async def extract(self, text: str, ctx: ExtractionContext) -> Extraction:
        raise NotImplementedError


# --------------------------------------------------------------------------- rules

_VEHICLE_WORDS = r"(?:carro|veiculo|automovel|ele|ela|moto|caminhonete|suv)"
_AGE_PATTERNS = (
    re.compile(r"\btenho (\d{1,3}) ?(?:anos|a)\b"),
    re.compile(r"\bidade[:\s]*(?:e |de )?(\d{1,3})\b"),
    re.compile(r"\b(\d{1,3}) anos de idade\b"),
    re.compile(r"\b(\d{1,3}) (?:anos|aninhos)\b"),
)
_BIRTH_YEAR = re.compile(r"\bnasci (?:em|no ano de) (?:\d{1,2}/\d{1,2}/)?(19\d{2}|20\d{2})\b")
_YEAR = re.compile(r"(?<![\d/.-])(19[5-9]\d|20[0-4]\d)(?![\d/])")
_SHORT_YEAR_AFTER_MODEL = re.compile(r"^\s*(?:ano\s*)?'?(\d{2})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_ONLY = re.compile(r"\bdia (\d{1,2})\b")
_ZIP_BARE = re.compile(r"\bcep\D{0,5}(\d{8})\b")
_ZIP_MALFORMED = re.compile(r"\bcep\D{0,5}(\d[\d.-]{1,12}\d)\b")
_START_HINTS = re.compile(
    r"\b(inicio|iniciar|comecar|comece|comeca|vigencia|a partir|valer|ativar|contratar em)\b"
)

_INTENT_RULES: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (
        Intent.REQUEST_HUMAN,
        re.compile(
            r"\b(humano|atendente|pessoa de verdade|pessoa real|falar com (?:alguem|uma pessoa|"
            r"o vendedor|um vendedor|vendedor|corretor|consultor)|me liga|liga pra mim|"
            r"robo\b.*\bnao|nao (?:quero|gosto de) (?:falar com )?robo)"
        ),
    ),
    (
        Intent.OUT_OF_SCOPE,
        re.compile(
            r"\b(sinistro|bati o carro|batida|acidente|roubaram|furtaram|fui roubad|guincho|"
            r"cancelar (?:o |meu )?seguro|segunda via|boleto atrasado|reclamac|procon|"
            r"advogado|seguro de vida|seguro residencial|seguro viagem|minha moto|"
            r"caminhao)"
        ),
    ),
    (
        Intent.DECLINE,
        re.compile(
            r"\b(nao (?:quero|tenho interesse|precisa|vou querer)|deixa pra la|desisto|"
            r"sem interesse|vou ficar com a? ?outra|agora nao|nao, obrigad)"
        ),
    ),
    (
        Intent.ACCEPT,
        re.compile(
            r"\b(fechado|fechar|pode emitir|vamos nessa|bora|quero contratar|contratar|aceito|"
            r"quero (?:esse|este|ele)|pode fazer|pode seguir|manda o boleto|fechou)\b"
        ),
    ),
    (
        Intent.OBJECTION,
        re.compile(
            r"\b(caro|salgado|mais barato|concorrente|franquia (?:ta|esta|e) alta|"
            r"preciso pensar|vou pensar|ver com (?:minha |meu )?(?:esposa|marido|familia)|"
            r"desconto|abaixar|baixar)\b"
        ),
    ),
    (
        Intent.ASK_DETAILS,
        re.compile(r"\b(o que cobre|cobre |cobertura|franquia|carencia|como funciona)"),
    ),
    (
        Intent.GREETING,
        re.compile(r"^(oi+|ola|bom dia|boa tarde|boa noite|eae|e ai|opa|hello)\b"),
    ),
)
_AFFIRMATIVE = re.compile(
    r"^(sim|s|ok|okay|pode ser|pode mandar|manda|isso|claro|beleza|blz|perfeito|quero)\b"
)
_ORDINALS = {"primeiro": 0, "1": 0, "segundo": 1, "2": 1, "terceiro": 2, "3": 2}

_MODEL_INDEX: list[tuple[str, str, str]] = sorted(
    (
        (normalize(model), make, model)
        for make, models in MODELS_BY_MAKE.items()
        for model in models
    ),
    key=lambda t: -len(t[0]),
)


class RuleBasedExtractor(Extractor):
    async def extract(self, text: str, ctx: ExtractionContext) -> Extraction:
        return self.extract_sync(text, ctx)

    def extract_sync(self, text: str, ctx: ExtractionContext) -> Extraction:
        pii = find_pii(text)
        zip_code = next((m.value for m in pii if m.kind is PiiKind.ZIP), None)
        # Remove every PII span so CPF/phone digits are never read as age/year.
        scrubbed = text
        for m in sorted(pii, key=lambda m: -m.start):
            scrubbed = scrubbed[: m.start] + " " + scrubbed[m.end :]
        norm = normalize(scrubbed)
        expected = set(ctx.expected_fields)

        if zip_code is None:
            bare = _ZIP_BARE.search(normalize(text))
            if bare:
                zip_code = bare.group(1)
            else:
                # "cep 0131": keep the malformed value so validation can explain the error.
                malformed = _ZIP_MALFORMED.search(normalize(text))
                zip_code = malformed.group(1) if malformed else None
        if zip_code is None and LeadField.ZIP_CODE in expected:
            digits = re.sub(r"\D", "", text)
            if len(digits) == 8 and len(text.strip()) <= 12:
                zip_code = digits

        start_date = _extract_start_date(norm, ctx, expected)
        dateless = _DATE_ISO.sub(" ", _DATE_DMY.sub(" ", norm))
        model, make = _extract_model(dateless)
        year = _extract_vehicle_year(dateless, model)
        age = _extract_age(dateless, ctx.today)
        if age is None and year is None and LeadField.AGE in expected:
            bare_number = re.fullmatch(r"\s*(\d{2,3})\s*", norm)
            if bare_number:
                age = int(bare_number.group(1))
        if age is not None and year is not None and str(age) in str(year):
            age = None  # "Corolla 2008" must not yield age=20 or 08
        plan_id = _extract_plan(norm, ctx)
        intent = _extract_intent(norm, ctx)
        result = Extraction(
            intent=intent,
            age=age,
            vehicle_model=f"{make} {model}" if model and make else None,
            vehicle_year=year,
            zip_code=zip_code,
            start_date=start_date,
            plan_id=plan_id,
            source="rules",
        )
        if result.intent is Intent.OTHER and result.has_fields():
            result = replace(result, intent=Intent.PROVIDE_INFO)
        return result


def _extract_model(norm: str) -> tuple[str | None, str | None]:
    for key, make, model in _MODEL_INDEX:
        if key.isdigit() and normalize(make) not in norm:
            continue  # "Gol 2008" is a year, not a Peugeot 2008
        if re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", norm):
            return model, make
    for alias, make in {**MAKE_ALIASES, **{normalize(m): m for m in MODELS_BY_MAKE}}.items():
        if re.search(rf"\b{re.escape(alias)}\b", norm):
            return None, make
    return None, None


def _extract_vehicle_year(norm: str, model: str | None) -> int | None:
    if _BIRTH_YEAR.search(norm):
        norm = _BIRTH_YEAR.sub(" ", norm)
    years = [int(y) for y in _YEAR.findall(norm)]
    if years:
        return years[0]
    if model:  # "Gol 14" / "Onix ano 19"
        tail = norm.split(normalize(model), 1)[-1]
        short = _SHORT_YEAR_AFTER_MODEL.match(tail)
        if short:
            return 2000 + int(short.group(1))
    return None


def _extract_age(norm: str, today: date) -> int | None:
    birth = _BIRTH_YEAR.search(norm)
    if birth:
        return today.year - int(birth.group(1))
    for pattern in _AGE_PATTERNS:
        for m in pattern.finditer(norm):
            before = norm[max(0, m.start() - 25) : m.start()]
            if re.search(rf"\b{_VEHICLE_WORDS}\b", before) and "tenho" not in m.group(0):
                continue  # "o carro tem 5 anos" is a vehicle age, not the driver's
            return int(m.group(1))
    return None


def _extract_plan(norm: str, ctx: ExtractionContext) -> str | None:
    for plan_id in ctx.plan_ids:
        if re.search(rf"\b{re.escape(normalize(plan_id))}\b", norm):
            if plan_id == "completo" and re.search(r"\bnome completo\b", norm):
                continue
            return plan_id
    if ctx.state in (ConversationState.PLAN_SELECTION, ConversationState.PRESENTING):
        if re.search(r"\b(mais barato|basico|economico)\b", norm):
            return ctx.plan_ids[0] if ctx.plan_ids else None
        if re.search(r"\b(mais completo|top|melhor)\b", norm):
            return ctx.plan_ids[-1] if ctx.plan_ids else None
    if ctx.state is ConversationState.PLAN_SELECTION:
        m = re.search(r"\b(primeiro|segundo|terceiro|[123])\b", norm)
        if m and _ORDINALS[m.group(1)] < len(ctx.plan_ids):
            return ctx.plan_ids[_ORDINALS[m.group(1)]]
    return None


def _extract_intent(norm: str, ctx: ExtractionContext) -> Intent:
    for intent, pattern in _INTENT_RULES:
        if pattern.search(norm):
            return intent
    if ctx.state is ConversationState.PRESENTING and _AFFIRMATIVE.search(norm):
        return Intent.ACCEPT
    return Intent.OTHER


def _extract_start_date(norm: str, ctx: ExtractionContext, expected: set[LeadField]) -> date | None:
    if LeadField.START_DATE not in expected and not _START_HINTS.search(norm):
        return None
    today = ctx.today
    iso = _DATE_ISO.search(norm)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    dmy = _DATE_DMY.search(norm)
    if dmy:
        day, month = int(dmy.group(1)), int(dmy.group(2))
        year_txt = dmy.group(3)
        if year_txt:
            year = int(year_txt) + (2000 if len(year_txt) == 2 else 0)
            return _safe_date(year, month, day)
        candidate = _safe_date(today.year, month, day)
        if candidate and candidate < today:
            candidate = _safe_date(today.year + 1, month, day)
        return candidate
    day_only = _DAY_ONLY.search(norm)
    if day_only:
        day = int(day_only.group(1))
        candidate = _safe_date(today.year, today.month, day)
        if candidate is None or candidate < today:
            nxt = _first_of_next_month(today)
            candidate = _safe_date(nxt.year, nxt.month, day)
        return candidate
    return _relative_start_date(norm, today)


def _relative_start_date(norm: str, today: date) -> date | None:
    if re.search(r"\bdepois de amanha\b", norm):
        return today + timedelta(days=2)
    if re.search(r"\bamanha\b", norm):
        return today + timedelta(days=1)
    if re.search(r"\b(proximo mes|mes que vem)\b", norm):
        return _first_of_next_month(today)
    if re.search(r"\b(hoje|agora|imediatamente|o quanto antes|o mais rapido)\b", norm):
        return today
    return None


def _first_of_next_month(today: date) -> date:
    days = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=1) + timedelta(days=days)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------- hybrid


class HybridExtractor(Extractor):
    def __init__(self, rules: RuleBasedExtractor, llm: LLMClient | None) -> None:
        self._rules = rules
        self._llm = llm

    async def extract(self, text: str, ctx: ExtractionContext) -> Extraction:
        rules = self._rules.extract_sync(text, ctx)
        if self._llm is None or not _rules_inconclusive(rules):
            return rules
        masked = mask_text(text, ctx.known_names)
        system, user = build_extraction_prompt(masked, ctx)
        try:
            data = await self._llm.complete_json(system, user)
        except LLMUnavailableError as exc:
            log.warning("llm_fallback_to_rules", reason=str(exc), llm=self._llm.name)
            return rules
        llm_result = parse_llm_extraction(data, ctx)
        log.info(
            "llm_extraction",
            llm=self._llm.name,
            prompt_version=EXTRACTION_PROMPT_VERSION,
            intent=llm_result.intent.value,
            fields=[
                k
                for k in ("age", "vehicle_model", "vehicle_year", "plan_id", "start_date")
                if getattr(llm_result, k) is not None
            ],
        )
        return merge_extractions(rules, llm_result)


def _rules_inconclusive(rules: Extraction) -> bool:
    return not rules.has_fields() and rules.intent in (Intent.OTHER, Intent.GREETING)


def parse_llm_extraction(data: dict[str, Any], ctx: ExtractionContext) -> Extraction:
    """Type-checks LLM output. Anything malformed is dropped, never trusted."""

    def as_int(key: str) -> int | None:
        value = data.get(key)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None

    def as_str(key: str) -> str | None:
        value = data.get(key)
        return value.strip()[:60] if isinstance(value, str) and value.strip() else None

    start: date | None = None
    raw_date = as_str("start_date")
    if raw_date:
        try:
            start = date.fromisoformat(raw_date)
        except ValueError:
            start = None
    plan = as_str("plan_id")
    plan = plan.lower() if plan and plan.lower() in ctx.plan_ids else None
    try:
        intent = Intent(str(data.get("intent", "other")).lower())
    except ValueError:
        intent = Intent.OTHER
    return Extraction(
        intent=intent,
        age=as_int("age"),
        vehicle_model=as_str("vehicle_model"),
        vehicle_year=as_int("vehicle_year"),
        start_date=start,
        plan_id=plan,
        source="llm",
    )


def merge_extractions(rules: Extraction, llm: Extraction) -> Extraction:
    def pick(name: str) -> Any:
        value = getattr(rules, name)
        return value if value is not None else getattr(llm, name)

    intent = rules.intent if rules.intent in _SAFETY_INTENTS else llm.intent
    if intent is Intent.OTHER:
        intent = rules.intent
    merged = Extraction(
        intent=intent,
        age=pick("age"),
        vehicle_model=pick("vehicle_model"),
        vehicle_year=pick("vehicle_year"),
        zip_code=rules.zip_code,  # only rules ever see the raw CEP
        start_date=pick("start_date"),
        plan_id=pick("plan_id"),
        source="rules+llm",
    )
    if merged.intent in (Intent.OTHER, Intent.GREETING) and merged.has_fields():
        merged = replace(merged, intent=Intent.PROVIDE_INFO)
    return merged
