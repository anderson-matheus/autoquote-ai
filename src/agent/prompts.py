"""Versioned LLM prompts. The version is logged with every LLM call for auditability."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent.extraction import ExtractionContext

EXTRACTION_PROMPT_VERSION = "extraction-v1"

_SYSTEM = """You extract structured data from Brazilian Portuguese WhatsApp messages sent by
a lead who wants a CAR insurance quote. Reply with ONE JSON object and nothing else.

Keys (use null when the message does not state the value; NEVER guess):
- "age": integer, the driver's age in years.
- "vehicle_model": string, make and model as written (e.g. "Toyota Corolla").
- "vehicle_year": integer, 4-digit model year of the car.
- "plan_id": one of {plan_ids} or null.
- "start_date": "YYYY-MM-DD" when the lead says when coverage should start. Today is {today}.
- "intent": one of ["provide_info", "greeting", "request_human", "accept", "decline",
  "objection", "ask_details", "out_of_scope", "other"].

Intent guide: "request_human" = wants a person/seller; "accept" = agrees to buy the quoted
plan; "decline" = not interested; "objection" = price/deductible complaints or hesitation;
"ask_details" = questions about coverage/deductible/waiting period; "out_of_scope" =
claims, accidents, cancellations, other insurance types, complaints.
Personal data was replaced by placeholders like [CPF] or [PHONE]; ignore them.

Examples:
msg: "e um Sandero 2022" -> {{"vehicle_model": "Renault Sandero", "vehicle_year": 2022,
"intent": "provide_info", "age": null, "plan_id": null, "start_date": null}}
msg: "achei caro pra esse carro" -> {{"intent": "objection", "age": null,
"vehicle_model": null, "vehicle_year": null, "plan_id": null, "start_date": null}}
msg: "quero falar com uma pessoa" -> {{"intent": "request_human", "age": null,
"vehicle_model": null, "vehicle_year": null, "plan_id": null, "start_date": null}}"""


def build_extraction_prompt(masked_text: str, ctx: ExtractionContext) -> tuple[str, str]:
    system = _SYSTEM.format(plan_ids=json.dumps(list(ctx.plan_ids)), today=ctx.today.isoformat())
    user = json.dumps(
        {
            "conversation_state": ctx.state.value,
            "fields_we_are_waiting_for": [f.value for f in ctx.expected_fields],
            "message": masked_text,
        },
        ensure_ascii=False,
    )
    return system, user
