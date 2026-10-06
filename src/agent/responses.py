"""Renders FSM replies into the customer-facing pt-BR WhatsApp text.

Customer-facing copy is the only Portuguese in the codebase (leads are Brazilian).
Prices are formatted exclusively from `Quote` objects returned by the quote service:
there is no code path that can produce a number the API did not return.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from src.agent.fsm import Reply
from src.domain.models import HandoffReason, LeadField, Plan, Quote

_FIELD_QUESTIONS: dict[LeadField, str] = {
    LeadField.VEHICLE_YEAR: "o *modelo e ano* do carro",
    LeadField.AGE: "a *idade* do principal condutor",
    LeadField.ZIP_CODE: "o *CEP* onde o carro passa a noite",
    LeadField.START_DATE: "a partir de *quando* quer o seguro valendo (ex.: hoje, dia 15, 01/11)",
}

_COVERAGE_LABELS: dict[str, str] = {
    "colisao": "colisão",
    "roubo": "roubo",
    "furto": "furto",
    "terceiros": "danos a terceiros",
    "vidros": "vidros",
    "carro_reserva": "carro reserva",
    "assistencia_24h": "assistência 24h",
}


def money(value: Decimal) -> str:
    """pt-BR currency: Decimal('1234.5') -> 'R$ 1.234,50'."""
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {text}"


def coverages(items: Sequence[str]) -> str:
    labels = [_COVERAGE_LABELS.get(c, c.replace("_", " ")) for c in items]
    if len(labels) <= 1:
        return "".join(labels)
    return f"{', '.join(labels[:-1])} e {labels[-1]}"


def _ask_fields(fields: Sequence[LeadField]) -> str:
    asks = [_FIELD_QUESTIONS[f] for f in fields]
    joined = asks[0] if len(asks) == 1 else f"{', '.join(asks[:-1])} e {asks[-1]}"
    return f"Pra calcular certinho, me passa {joined}?"


def _plan_menu(plans: Sequence[Plan]) -> str:
    lines = ["Tenho estes planos pra você:"]
    for i, p in enumerate(plans, start=1):
        lines.append(
            f"{i}. *{p.name}* — cobre {coverages(p.coverages)}; franquia {money(p.deductible)}"
        )
    lines.append("Qual deles você quer cotar?")
    return "\n".join(lines)


def _quote(q: Quote) -> str:
    lines = [
        f"Sua cotação do plano *{q.plan_name}* ficou em *{money(q.monthly_premium)}/mês*.",
        f"• Coberturas: {coverages(q.coverages)}",
        f"• Franquia: {money(q.deductible)}",
    ]
    if q.waiting_period_coverages:
        lines.append(
            f"• Carência: {coverages(q.waiting_period_coverages)} passam a valer "
            f"{q.waiting_period_days} dias após o início da vigência"
        )
    if q.pro_rata:
        lines.append(
            f"• Como a vigência começa no meio do mês, o 1º pagamento é proporcional: "
            f"{money(q.pro_rata.first_payment)} ({q.pro_rata.days_charged} de "
            f"{q.pro_rata.days_in_month} dias). Depois, {money(q.monthly_premium)}/mês."
        )
    lines.append("Quer contratar? Também posso cotar outro plano ou te passar pra um consultor.")
    return "\n".join(lines)


def _details(q: Quote) -> str:
    text = (
        f"O *{q.plan_name}* cobre {coverages(q.coverages)}, com franquia de {money(q.deductible)}."
    )
    if q.waiting_period_coverages:
        text += (
            f" {coverages(q.waiting_period_coverages).capitalize()} têm carência de "
            f"{q.waiting_period_days} dias."
        )
    return text + " Quer seguir com ele?"


def _offer_cheaper(plan: Plan | None) -> str:
    if plan is None:
        return "Entendo! Vou ver com um consultor uma condição melhor pra você."
    return (
        f"Entendo! Uma opção mais em conta é o *{plan.name}* (cobre {coverages(plan.coverages)}, "
        f"franquia {money(plan.deductible)}). Quer que eu cote ele?"
    )


_HANDOFF: dict[HandoffReason, Callable[[str | None], str]] = {
    HandoffReason.CUSTOMER_REQUEST: lambda _: (
        "Claro! Já chamei um consultor, ele continua seu atendimento por aqui em instantes."
    ),
    HandoffReason.PLAN_ACCEPTED: lambda _: (
        "Ótima escolha! 🎉 Um consultor vai finalizar a contratação com você por aqui "
        "(emissão da apólice e forma de pagamento)."
    ),
    HandoffReason.QUOTE_UNAVAILABLE: lambda _: (
        "Nosso sistema de cotação está instável agora e eu não quero te passar um valor "
        "errado. Já deixei seus dados com um consultor, que vai te mandar a cotação por aqui. "
        "Se o sistema voltar antes, eu mesmo te envio."
    ),
    HandoffReason.UNDERWRITING_REFUSAL: lambda detail: (
        "Infelizmente não consigo cotar automaticamente nesse caso"
        + (f" ({detail.rstrip('.')})" if detail else "")
        + ". Passei seu atendimento pra um consultor avaliar as alternativas com você."
    ),
    HandoffReason.NEGOTIATION: lambda _: (
        "Entendo! Vou chamar um consultor que pode avaliar condições especiais pra você."
    ),
    HandoffReason.COLLECTION_STALLED: lambda _: (
        "Acho que não estou conseguindo te ajudar direito por aqui. Vou te passar pra um "
        "consultor, tá bom?"
    ),
    HandoffReason.OUT_OF_SCOPE: lambda _: (
        "Esse assunto precisa de um especialista. Já chamei um consultor pra te atender."
    ),
    HandoffReason.SYSTEM_ERROR: lambda _: (
        "Tive um problema técnico pra gerar sua cotação. Um consultor já foi avisado e "
        "continua com você por aqui."
    ),
}

_STATIC: dict[str, str] = {
    "greeting": "Olá! Sou o assistente virtual da AutoSeguro 🚗 Vou fazer sua cotação rapidinho.",
    "welcome_back": "Que bom te ver de novo! Vamos continuar sua cotação.",
    "quoting": "Perfeito, estou calculando sua cotação…",
    "requoting": "Certo, vou recalcular com essa informação…",
    "quote_delayed": "Só um instante, o sistema de cotação está um pouco lento…",
    "quote_recovered": "Boa notícia: o sistema voltou e consegui calcular sua cotação!",
    "next_steps": (
        "Posso seguir com a contratação, cotar outro plano (Essencial, Completo ou Premium) "
        "ou te passar pra um consultor. O que prefere?"
    ),
    "farewell": "Sem problemas! Se mudar de ideia é só mandar uma mensagem por aqui. 👋",
    "handoff_ack": "Seu atendimento já está com um consultor, ele te responde por aqui em breve.",
}


def render(reply: Reply) -> str:
    p: dict[str, Any] = reply.params
    match reply.key:
        case "ask_fields":
            return _ask_fields(p["fields"])
        case "invalid_field":
            return f"Hmm, {p['message']}. Pode conferir?"
        case "plan_menu":
            return _plan_menu(p["plans"])
        case "quote_presented":
            return _quote(p["quote"])
        case "quote_details":
            return _details(p["quote"])
        case "offer_cheaper":
            return _offer_cheaper(p["plan"])
        case "media_not_supported":
            return "Ainda não consigo abrir anexos ou áudios por aqui. Pode me mandar por texto?"
        case key if key.startswith("handoff_") and key != "handoff_ack":
            return _HANDOFF[HandoffReason(key.removeprefix("handoff_"))](p.get("detail"))
        case key:
            return _STATIC[key]
