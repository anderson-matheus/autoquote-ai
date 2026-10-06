from __future__ import annotations

import json
from pathlib import Path

from scripts.export_ai_logs import export, sanitize_text


def test_secrets_and_pii_are_redacted() -> None:
    # Fake keys are assembled at runtime so secret scanners don't flag this file.
    groq_key, openai_key = "gsk" + "_" + "a" * 32, "sk" + "-proj-" + "B" * 20
    text = (
        f"key {groq_key} and {openai_key} "
        "Authorization: Bearer abc.def.ghijklmnop LLM_API_KEY=realvalue123 "
        "CONTACT_HASH_SECRET=change-me cpf 389.083.863-43 mail dev@example.com"
    )
    out = sanitize_text(text)
    for leaked in (
        "gsk_abc",
        "sk-proj-ABC",
        "abc.def.ghij",
        "realvalue123",
        "389.083",
        "dev@example.com",
    ):
        assert leaked not in out
    assert "CONTACT_HASH_SECRET=change-me" in out  # documented placeholder stays readable


def test_export_keeps_valid_json(tmp_path: Path) -> None:
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    (src / "s.jsonl").write_text(
        json.dumps({"ts": 1760000000, "msg": "cpf 389.083.863-43", "name": "Bash"}) + "\n\n"
        "not json\n",
        encoding="utf-8",
    )
    [out] = export(src, dest)
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines[0] == {"ts": 1760000000, "msg": "cpf [CPF]", "name": "Bash"}
    assert "unparseable_line" in lines[1]
