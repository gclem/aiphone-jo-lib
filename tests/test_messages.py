"""Cloud envelopes: control, signaling, relay, SDP and call signals."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime

import pytest

from aiphone_jo import CloudCredentials
from aiphone_jo.exceptions import InvalidCredentialsError, InvalidInputError
from aiphone_jo.messages import (
    CONTROL_COMMANDS,
    build_call_signal,
    build_control_request,
    build_disconnect_reply,
    build_disconnect_request,
    build_get_lock_status_request,
    build_lock_request,
    build_monitor_request,
    build_out_option_request,
    build_relay_request,
    build_relay_sdp,
    build_synthetic_relay_sdp,
    build_unlock_request,
)

from .conftest import CLIENT_ID, RECV_TOPIC, SEND_TOPIC

NOW = datetime(2024, 3, 5, 10, 20, 30, 123000)
CID = "client-abc20240305102030123000"


def test_unlock_publishes_on_send_topic_with_exact_header(credentials: CloudCredentials) -> None:
    message = build_unlock_request(credentials, NOW)
    assert message.topic == SEND_TOPIC
    assert message.reply_mid == "27021"
    # Exact key order expected by the protocol.
    assert list(message.header) == ["MID", "CLIID", "RSLT", "RCVTPC", "VER", "SYS", "SID", "AVID"]
    assert message.header == {
        "MID": "26021",
        "CLIID": CLIENT_ID,
        "RSLT": 0,
        "RCVTPC": RECV_TOPIC,
        "VER": "1.2",
        "SYS": "4",
        "SID": "20240305102030123000",
        "AVID": 0,
    }
    assert message.payload["BODY"] == {}


def test_lock_has_the_unlock_shape_with_mid_26032(credentials: CloudCredentials) -> None:
    message = build_lock_request(credentials, NOW)
    assert message.mid == "26032"
    assert message.reply_mid == "27032"
    assert message.header["AVID"] == 0
    assert message.payload["BODY"] == {}


def test_get_lock_status_has_no_avid_nor_body(credentials: CloudCredentials) -> None:
    message = build_get_lock_status_request(credentials, NOW)
    assert message.topic == SEND_TOPIC
    assert message.mid == "26031"
    assert message.header["RCVTPC"] == RECV_TOPIC
    assert "AVID" not in message.header
    assert "BODY" not in message.payload


def test_out_option_has_the_unlock_envelope_with_mid_26121(credentials: CloudCredentials) -> None:
    unlock = build_unlock_request(credentials, NOW)
    option = build_out_option_request(credentials, NOW)
    assert option.topic == SEND_TOPIC
    assert option.reply_mid == "27121"
    assert option.payload["BODY"] == {}
    assert {**option.header, "MID": "26021"} == unlock.header


def test_build_control_request_reply_mid_and_danger(credentials: CloudCredentials) -> None:
    expected = {
        "unlock": ("27021", True),
        "get_lock_status": ("27031", False),
        "lock": ("27032", True),
        "out_option": ("27121", True),
    }
    for command, (reply_mid, dangerous) in expected.items():
        message, is_dangerous = build_control_request(command, credentials, NOW)
        assert (message.reply_mid, is_dangerous) == (reply_mid, dangerous)
    assert sorted(CONTROL_COMMANDS) == ["get_lock_status", "lock", "out_option", "unlock"]
    with pytest.raises(InvalidInputError, match="Unknown"):
        build_control_request("nope", credentials, NOW)


def test_incomplete_credentials_are_refused(credentials: CloudCredentials) -> None:
    with pytest.raises(InvalidCredentialsError, match="send_topic"):
        build_unlock_request(replace(credentials, send_topic=None), NOW)


def test_monitor_request(credentials: CloudCredentials) -> None:
    message = build_monitor_request(credentials, 3, 5, CID, NOW)
    assert message.topic == SEND_TOPIC
    assert message.reply_mid == "24021"
    assert list(message.header) == [
        "MID",
        "CLIID",
        "CID",
        "PCLIID",
        "NCLIID",
        "DSP1",
        "DSP2",
        "CKIND",
        "TMKIND",
        "TMID",
        "RECKIND",
        "AVID",
        "RSLT",
        "SNDTPC",
        "RCVTPC",
        "VER",
        "SYS",
        "SID",
    ]
    header = message.header
    assert (header["MID"], header["CID"], header["DSP1"], header["CKIND"]) == (
        "23021",
        CID,
        "Home",
        1,
    )
    assert (header["TMKIND"], header["TMID"]) == (5, 3)
    assert (header["SNDTPC"], header["RCVTPC"]) == (SEND_TOPIC, RECV_TOPIC)


def test_monitor_request_defaults_display_name(credentials: CloudCredentials) -> None:
    message = build_monitor_request(replace(credentials, term_name=None), 3, 5, CID, NOW)
    assert message.header["DSP1"] == "aiphone-jo"


def test_monitor_request_validation(credentials: CloudCredentials) -> None:
    with pytest.raises(InvalidInputError, match="nTermId"):
        build_monitor_request(credentials, 0, 5, CID, NOW)
    with pytest.raises(InvalidInputError, match="nTermKind"):
        build_monitor_request(credentials, 3, 0, CID, NOW)
    with pytest.raises(InvalidInputError, match="CID"):
        build_monitor_request(credentials, 3, 5, "", NOW)


def test_disconnect_request(credentials: CloudCredentials) -> None:
    message = build_disconnect_request(credentials, CID, NOW)
    assert message.topic == SEND_TOPIC
    assert message.reply_mid == "24003"
    header = message.header
    assert (header["MID"], header["CID"], header["CLIID"]) == ("23003", CID, CLIENT_ID)
    assert (header["REASON"], header["CKIND"]) == (16, 1)
    assert "TMID" not in header
    assert "TMKIND" not in header
    with pytest.raises(InvalidInputError, match="CID"):
        build_disconnect_request(credentials, "", NOW)


def test_disconnect_reply(credentials: CloudCredentials) -> None:
    message = build_disconnect_reply(credentials, CID, NOW)
    assert message.reply_mid is None
    assert (message.mid, message.header["RSLT"], message.header["REASON"]) == ("24003", 200, 0)
    assert message.header["CID"] == CID
    assert list(message.header) == list(build_disconnect_request(credentials, CID, NOW).header)


@pytest.mark.parametrize(
    ("signal", "mid", "rslt", "ckind", "reason", "reply"),
    [
        ("ready", "24001", 0, 0, 0, None),
        ("reply", "24002", 200, 3, 0, "25001"),
        ("start", "23008", 200, 3, 0, "24008"),
        ("reject", "23008", 603, 3, 0, None),
        ("cancel_reply", "24004", 200, 3, 16, None),
    ],
)
def test_call_signals(
    credentials: CloudCredentials,
    signal: str,
    mid: str,
    rslt: int,
    ckind: int,
    reason: int,
    reply: str | None,
) -> None:
    message = build_call_signal(credentials, signal, "call-1", call_kind=3, now=NOW)
    header = message.header
    assert (header["MID"], header["RSLT"], header["CKIND"], header["REASON"]) == (
        mid,
        rslt,
        ckind,
        reason,
    )
    assert (header["DSP1"], header["PCLIID"], header["CID"]) == ("", "", "call-1")
    assert message.reply_mid == reply
    with pytest.raises(InvalidInputError):
        build_call_signal(credentials, "nope", "call-1")


def test_relay_request_header_order_and_body(credentials: CloudCredentials) -> None:
    message = build_relay_request(
        "update_rcv_user", credentials, cid=CID, cor_cli_id="station", sdp="v=0\r\n", now=NOW
    )
    assert list(message.header) == [
        "MID",
        "CID",
        "CLIID",
        "PCLIID",
        "RSLT",
        "RCVTPC",
        "RLS",
        "VER",
        "SYS",
        "SID",
    ]
    assert (message.mid, message.reply_mid, message.header["RLS"]) == ("30012", "31012", "0")
    assert message.header["PCLIID"] == "station"
    assert message.payload["BODY"] == {"SDP": "v=0\r\n"}
    leave = build_relay_request("leave_user", credentials, cid=CID, now=NOW)
    assert (leave.mid, leave.reply_mid, leave.payload["BODY"]) == ("30031", "31031", {})


def test_relay_request_cid_rules(credentials: CloudCredentials) -> None:
    start = build_relay_request("sta_relay", credentials, sdp="v=0\r\n", now=NOW)
    assert start.header["CID"] == f"{CLIENT_ID}20240305102030123000"
    with pytest.raises(InvalidInputError, match="CID"):
        build_relay_request("add_rcv_user", credentials)
    with pytest.raises(InvalidInputError, match="SDP"):
        build_relay_request("sta_relay", credentials)
    with pytest.raises(InvalidInputError, match="Unknown"):
        build_relay_request("nope", credentials, cid=CID)


def test_encode_is_compact_utf8(credentials: CloudCredentials) -> None:
    message = build_monitor_request(replace(credentials, term_name="Entrée"), 3, 5, CID, NOW)
    encoded = message.encode()
    assert b'"DSP1":"Entr\xc3\xa9e"' in encoded
    assert b", " not in encoded
    assert json.loads(encoded) == message.payload


def test_relay_sdp_attribute_order() -> None:
    sdp = build_relay_sdp(
        fingerprint="sha-256 AA:BB",
        ufrag="uf",
        pwd="pw",
        candidates=["a=candidate:1 1 UDP 1 192.0.2.1 9 typ host"],
        audio_codecs=(1, 2),
        video_codecs=(1, 0),
        audio_payload_types=(-1, 18),
        fec_payload_type=127,
        direction="sendonly",
        ssrc=42,
        now=NOW,
    )
    lines = sdp.split("\r\n")
    assert sdp.endswith("\r\n")
    assert lines[:10] == [
        "v=0",
        f"o=- {int(NOW.timestamp())} 1 IN IP4 0.0.0.0",
        "s=-",
        "c=IN IP4 0.0.0.0",
        "t=0 0",
        "a=group:BUNDLE audio video",
        "a=ice-ufrag:uf",
        "a=ice-pwd:pw",
        "a=fingerprint:sha-256 AA:BB",
        "a=setup:actpass",
    ]
    assert "m=audio 9 UDP/TLS/RTP/SAVP 0 18 127" in lines
    assert "a=rtpmap:18 PCMA/8000" in lines
    assert "a=rtpmap:127 ulpfec/8000" in lines
    assert "a=ssrc:42" in lines
    assert "m=video 9 UDP/TLS/RTP/SAVP 107 127" in lines
    assert any(line.startswith("a=fmtp:107 level-asymmetry-allowed=1") for line in lines)
    assert lines.count("a=candidate:1 1 UDP 1 192.0.2.1 9 typ host") == 2


def test_synthetic_sdp_matches_relay_probe() -> None:
    sdp = build_synthetic_relay_sdp(NOW)
    assert "m=audio 51712 UDP/TLS/RTP/SAVP 0" in sdp
    assert "m=video" not in sdp
    assert "a=sendonly" in sdp
    assert "a=candidate:1 1 UDP 2130706431 192.0.2.10 51712 typ host" in sdp
    assert re.search(r"a=fingerprint:sha-256 ([0-9A-F]{2}:){31}[0-9A-F]{2}\r\n", sdp)
    assert re.search(r"a=ice-ufrag:[0-9a-f]{8}\r\n", sdp)
    assert re.search(r"a=ice-pwd:[0-9a-f]{32}\r\n", sdp)
    assert build_synthetic_relay_sdp(NOW) != sdp
