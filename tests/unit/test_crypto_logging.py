from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.utils.crypto import FieldCipher, contact_hash
from src.utils.logging import bind_trace_id, clear_context, configure_logging, get_logger


def test_cipher_roundtrip_and_tamper_detection() -> None:
    cipher = FieldCipher.ephemeral()
    token = cipher.encrypt("5511999990000")
    assert "5511999990000" not in token
    assert cipher.decrypt(token) == "5511999990000"
    with pytest.raises(ValueError, match="cannot decrypt"):
        FieldCipher.ephemeral().decrypt(token)


def test_dev_cipher_is_deterministic_per_passphrase() -> None:
    token = FieldCipher.from_passphrase("a").encrypt("x")
    assert FieldCipher.from_passphrase("a").decrypt(token) == "x"


def test_contact_hash_is_keyed() -> None:
    assert contact_hash("5511", "k1") == contact_hash("5511", "k1")
    assert contact_hash("5511", "k1") != contact_hash("5511", "k2")


def test_logs_are_json_with_trace_id_and_masked(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "execution.log"
    configure_logging("INFO", str(log_file), stream=False)
    try:
        bind_trace_id("trace0001")
        get_logger("t").info("message_received", body="cpf 389.083.863-43", sender_name="Ana")
        logging.getLogger("stdlib").warning("raw email ana@x.com")
    finally:
        clear_context()
        for handler in logging.getLogger().handlers:
            handler.flush()
    lines = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert lines[0]["trace_id"] == "trace0001"
    assert lines[0]["body"] == "cpf [CPF]"
    assert lines[0]["sender_name"] == "[REDACTED]"
    assert "ana@x.com" not in log_file.read_text()
