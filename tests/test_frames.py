"""Wire format, redaction and input validation."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from aiphone_jo._util import (
    create_call_id,
    format_fingerprint,
    normalize_fingerprint,
    private_address,
    require_text,
    seq_id,
)
from aiphone_jo.exceptions import InvalidInputError, ProtocolError
from aiphone_jo.frames import JsonLinesDecoder, frame, parse_frame
from aiphone_jo.redact import MASK, PEM_MASK, redact

from .conftest import Pki


def test_frame_serialization() -> None:
    assert frame("01001") == b'{"HEADER":{"MID":"01001","VER":"1.0"}}\n'
    assert frame("01003", {"OTPASS": "1234"}) == (
        b'{"HEADER":{"MID":"01003","VER":"1.0"},"BODY":{"OTPASS":"1234"}}\n'
    )


def test_json_lines_fragmented_utf8_and_multiple_frames() -> None:
    decoder = JsonLinesDecoder()
    data = frame("11005", {"RSLT": 0, "TERMNM": "Entrée"}) + frame("11002", {"RSLT": 0})
    received = []
    for byte in data:
        received += decoder.push(bytes([byte]))
    assert received[0]["BODY"]["TERMNM"] == "Entrée"
    assert len(received) == 2


def test_json_lines_bounded_buffers() -> None:
    with pytest.raises(ProtocolError, match="too large"):
        JsonLinesDecoder(max_bytes=4).push(b"12345")
    with pytest.raises(ProtocolError, match="too large"):
        JsonLinesDecoder(max_bytes=4).push(b"12345\n")


@pytest.mark.parametrize(
    "text",
    ['{"HEADER":{"MID":11001}}', '{"HEADER":{"MID":"1100"}}', "[]", '{"BODY":{}}', "{invalid"],
)
def test_parse_frame_rejects_invalid_headers(text: str) -> None:
    with pytest.raises(ProtocolError):
        parse_frame(text)


def test_secrets_are_recursively_masked(pki: Pki) -> None:
    masked = redact(
        {
            "BODY": {
                "OTPASS": "secret-code",
                "nested": {"secret_key": pki.key_pem, "client_certificate": pki.cert_pem},
                "other": pki.cert_pem,
            },
            "visible": "ok",
        }
    )
    text = json.dumps(masked)
    assert masked["visible"] == "ok"
    assert "secret-code" not in text
    assert "BEGIN" not in text
    assert masked["BODY"]["other"] == PEM_MASK


def test_identifier_mode_masks_cloud_identity() -> None:
    message = {"HEADER": {"MID": "23021", "CLIID": "abc", "CID": "abc2024", "RCVTPC": "t/abc"}}
    assert redact(message)["HEADER"]["CLIID"] == "abc"
    strict = redact(message, identifiers=True)["HEADER"]
    assert strict == {"MID": "23021", "CLIID": MASK, "CID": MASK, "RCVTPC": MASK}


def test_redact_bounds_depth_and_length() -> None:
    deep: dict[str, object] = {}
    level = deep
    for _ in range(20):
        level["x"] = {}
        level = level["x"]  # type: ignore[assignment]
    assert "depth limited" in json.dumps(redact(deep))
    assert redact("a" * 3000).endswith("[truncated]")


def test_private_address() -> None:
    assert private_address("192.168.0.10") == "192.168.0.10"
    for value in ("192.0.2.10", "127.0.0.1", "172.32.0.2", "https://192.168.0.1", 3):
        with pytest.raises(InvalidInputError):
            private_address(value)


def test_require_text() -> None:
    assert require_text("  name ", "x") == "name"
    for value in ("", "   ", "a\nb", None, "x" * 13):
        with pytest.raises(InvalidInputError):
            require_text(value, "x", 12)


def test_seq_id_and_call_id_format() -> None:
    now = datetime(2024, 3, 5, 10, 20, 30, 123456)
    assert seq_id(now) == "20240305102030123456"
    assert create_call_id("client-abc", now) == "client-abc20240305102030123456"
    with pytest.raises(InvalidInputError):
        create_call_id("", now)


def test_fingerprints() -> None:
    digest = bytes(range(32))
    formatted = format_fingerprint(digest)
    assert formatted.startswith("00:01:02")
    assert normalize_fingerprint(formatted) == digest.hex()
    with pytest.raises(InvalidInputError):
        normalize_fingerprint("00" * 31)
