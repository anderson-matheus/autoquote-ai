"""Offline evaluation of the extractor against the challenge dataset.

The dataset labels each conversation with the lead's real age (`lead_idade_informada`)
and vehicle (`veiculo_texto`), so we replay every lead message through the same
extractor the agent uses and measure how often the final profile matches the labels.
It also verifies that masking removes every CPF/e-mail/phone/plate in the lead messages.

    uv run --group eval python -m scripts.eval_extraction [--limit 500]
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

import polars as pl

from src.agent.extraction import ExtractionContext, RuleBasedExtractor
from src.domain.models import ConversationState as S
from src.domain.models import LeadProfile
from src.utils.pii import mask_text

_RAW_PII = re.compile(
    r"\d{3}\.\d{3}\.\d{3}-\d{2}|[\w.+-]+@[\w-]+\.[\w.]+|\+55 \d{2} 9\d{4}-\d{4}|"
    r"\b[A-Z]{3}\d[A-Z]\d{2}\b"
)


def evaluate(path: Path, limit: int | None) -> dict[str, object]:
    df = pl.read_parquet(path).sort(["conversation_id", "message_index"])
    conv_ids = df["conversation_id"].unique(maintain_order=True)
    if limit:
        conv_ids = conv_ids.head(limit)
    extractor = RuleBasedExtractor()
    hits: Counter[str] = Counter()
    misses: list[str] = []
    pii_leaks = 0
    objections = Counter[str]()
    total = 0
    # Ages are computed against the date the conversations happened (dataset is 2026).
    today = date(2026, 6, 1)

    for cid in conv_ids:
        rows = df.filter(pl.col("conversation_id") == cid)
        first = rows.row(0, named=True)
        make, model, year = _split_vehicle(first["veiculo_texto"])
        profile = LeadProfile()
        quoted_by_seller = False
        for row in rows.iter_rows(named=True):
            if row["sender_role"] == "vendedor":
                quoted_by_seller |= "R$" in row["message_body"]
                continue
            if row["message_type"] != "text":
                continue
            body = row["message_body"]
            if _RAW_PII.search(mask_text(body)):
                pii_leaks += 1
            state = S.PRESENTING if quoted_by_seller else S.COLLECTING
            ex = extractor.extract_sync(
                body, ExtractionContext(state, today, tuple(profile.missing_fields()))
            )
            if quoted_by_seller:
                objections[ex.intent.value] += 1
            profile = profile.merge(
                age=ex.age,
                vehicle_year=ex.vehicle_year,
                vehicle_model=ex.vehicle_model,
                zip_code=ex.zip_code,
            )
        total += 1
        checks = {
            "age": profile.age == first["lead_idade_informada"],
            "vehicle_year": profile.vehicle_year == year,
            "vehicle_model": profile.vehicle_model == f"{make} {model}",
            "zip_code": profile.zip_code is not None,
        }
        for name, ok in checks.items():
            hits[name] += ok
        if not all(checks.values()) and len(misses) < 10:
            misses.append(
                f"{cid}: {first['veiculo_texto']!r} age={first['lead_idade_informada']}"
                f" -> {replace(profile, zip_code='***')}"
            )
    return {
        "conversations": total,
        "accuracy": {k: round(100 * v / total, 2) for k, v in hits.items()},
        "messages_with_unmasked_pii": pii_leaks,
        "intents_after_seller_quote": dict(objections.most_common()),
        "sample_misses": misses,
    }


def _split_vehicle(text: str) -> tuple[str, str, int]:
    *name, year = text.split()
    return name[0], " ".join(name[1:]), int(year)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("dataset/conversations.parquet"))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    if not args.dataset.exists():
        raise SystemExit("dataset not found: run `make dataset` first")
    report = evaluate(args.dataset, args.limit)
    for key, value in report.items():
        if key == "sample_misses":
            print("sample_misses:")
            for line in value:  # type: ignore[attr-defined]
                print(f"  - {line}")
        else:
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
