"""Pure SDP helpers (aiphone_jo.sdp)."""

from __future__ import annotations

import pytest

from aiphone_jo.sdp import (
    SDES_MID_EXTENSION,
    MediaSection,
    extract_section,
    media_sections,
    normalize_mids,
)

OFFER = "\r\n".join(
    [
        "v=0",
        "o=- 1 1 IN IP4 192.0.2.10",
        "s=VideoRoom 1",
        "t=0 0",
        "a=group:BUNDLE audio video",
        "a=ice-lite",
        "m=audio 9 UDP/TLS/RTP/SAVPF 0",
        "c=IN IP4 192.0.2.10",
        "a=sendrecv",
        "a=mid:audio",
        f"a=extmap:1 {SDES_MID_EXTENSION}",
        "a=rtpmap:0 PCMU/8000",
        "m=video 9 UDP/TLS/RTP/SAVPF 107",
        "c=IN IP4 192.0.2.10",
        "a=sendonly",
        "a=mid:video",
        f"a=extmap:1 {SDES_MID_EXTENSION}",
        "a=extmap:3 urn:3gpp:video-orientation",
        "a=rtpmap:107 H264/90000",
        "a=fmtp:107 profile-level-id=42e01f;packetization-mode=1",
        "",
    ]
)


def test_offer_is_rewritten_with_numeric_mids() -> None:
    offer, _ = normalize_mids(OFFER)
    lines = offer.split("\r\n")
    assert offer.endswith("\r\n")
    assert "a=group:BUNDLE 0 1" in lines
    assert [line for line in lines if line.startswith("a=mid:")] == ["a=mid:0", "a=mid:1"]
    assert SDES_MID_EXTENSION not in offer
    assert "a=extmap:3 urn:3gpp:video-orientation" in lines
    assert "a=fmtp:107 profile-level-id=42e01f;packetization-mode=1" in lines
    assert len(lines) == len(OFFER.split("\r\n")) - 2


def test_restore_puts_the_original_mids_back() -> None:
    _, restore = normalize_mids(OFFER)
    answer = "v=0\na=group:BUNDLE 0 1\nm=audio 9 UDP/TLS/RTP/SAVPF 0\na=mid:0\na=recvonly\n"
    answer += "m=video 9 UDP/TLS/RTP/SAVPF 107\na=mid:1\na=recvonly\n"
    restored = restore(answer)
    assert "a=group:BUNDLE audio video\n" in restored
    assert "a=mid:audio\n" in restored
    assert "a=mid:video\n" in restored
    assert restored.endswith("a=recvonly\n")
    assert restore("a=mid:9\r\n") == "a=mid:9\r\n"


def test_swapped_numeric_mids_are_not_chained() -> None:
    offer = "m=audio 9 RTP/AVP 0\na=mid:1\nm=video 9 RTP/AVP 96\na=mid:0\na=group:BUNDLE 1 0"
    normalized, restore = normalize_mids(offer)
    assert normalized == (
        "m=audio 9 RTP/AVP 0\na=mid:0\nm=video 9 RTP/AVP 96\na=mid:1\na=group:BUNDLE 0 1"
    )
    assert restore(normalized) == offer


def test_offer_without_mids_is_unchanged() -> None:
    offer = "v=0\r\nm=video 9 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
    normalized, restore = normalize_mids(offer)
    assert normalized == offer
    assert restore("a=mid:0\r\n") == "a=mid:0\r\n"


CALL_OFFER = "\r\n".join(
    [
        "v=0",
        "o=- 2 2 IN IP4 192.0.2.10",
        "s=VideoRoom 2",
        "t=0 0",
        "a=group:BUNDLE audio video data",
        "a=msid-semantic: WMS janus",
        "a=ice-lite",
        "a=fingerprint:sha-256 00:11:22",
        "m=audio 9 UDP/TLS/RTP/SAVPF 0 8",
        "c=IN IP4 192.0.2.10",
        "a=sendrecv",
        "a=mid:audio",
        "a=rtpmap:0 PCMU/8000",
        "m=video 9 UDP/TLS/RTP/SAVPF 107",
        "c=IN IP4 192.0.2.10",
        "a=sendonly",
        "a=mid:video",
        "a=rtpmap:107 H264/90000",
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel",
        "c=IN IP4 192.0.2.10",
        "a=mid:data",
        "",
    ]
)

AUDIO_ANSWER = "\r\n".join(
    [
        "v=0",
        "o=- 9 9 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE audio",
        "m=audio 40000 UDP/TLS/RTP/SAVPF 0",
        "c=IN IP4 192.0.2.20",
        "a=recvonly",
        "a=mid:audio",
        "a=rtpmap:0 PCMU/8000",
        "",
    ]
)


def test_media_sections_describe_each_section() -> None:
    sections = media_sections(CALL_OFFER)
    assert sections == [
        MediaSection(0, "audio", 9, "UDP/TLS/RTP/SAVPF", ("0", "8"), "audio", "sendrecv"),
        MediaSection(1, "video", 9, "UDP/TLS/RTP/SAVPF", ("107",), "video", "sendonly"),
        MediaSection(
            2, "application", 9, "UDP/DTLS/SCTP", ("webrtc-datachannel",), "data", "sendrecv"
        ),
    ]


def test_media_sections_use_the_session_direction_and_port_ranges() -> None:
    sdp = "v=0\na=recvonly\nm=audio 5000/2 RTP/AVP 0\nm=video 0 RTP/AVP 96\na=inactive\n"
    first, second = media_sections(sdp)
    assert (first.port, first.direction, first.mid) == (5000, "recvonly", None)
    assert (second.port, second.direction) == (0, "inactive")
    assert media_sections("v=0\n") == []


@pytest.mark.parametrize("line", ["m=audio", "m=audio x RTP/AVP 0"])
def test_media_sections_reject_invalid_m_lines(line: str) -> None:
    with pytest.raises(ValueError, match="Invalid"):
        media_sections(f"v=0\n{line}\n")


def test_extract_section_keeps_the_session_and_the_selected_section() -> None:
    offer, _ = extract_section(CALL_OFFER, "audio")
    lines = offer.split("\r\n")
    assert offer.endswith("\r\n")
    assert lines[:8] == [
        "v=0",
        "o=- 2 2 IN IP4 192.0.2.10",
        "s=VideoRoom 2",
        "t=0 0",
        "a=group:BUNDLE audio",
        "a=msid-semantic: WMS janus",
        "a=ice-lite",
        "a=fingerprint:sha-256 00:11:22",
    ]
    assert lines[8:-1] == [
        "m=audio 9 UDP/TLS/RTP/SAVPF 0 8",
        "c=IN IP4 192.0.2.10",
        "a=sendrecv",
        "a=mid:audio",
        "a=rtpmap:0 PCMU/8000",
    ]
    assert [section.kind for section in media_sections(offer)] == ["audio"]


def test_extract_section_selects_by_direction() -> None:
    offer, _ = extract_section(CALL_OFFER, "video", direction="sendonly")
    assert "a=group:BUNDLE video\r\n" in offer
    assert "m=audio" not in offer
    with pytest.raises(ValueError, match="video \\(recvonly\\)"):
        extract_section(CALL_OFFER, "video", direction="recvonly")
    with pytest.raises(ValueError, match="Unknown SDP direction"):
        extract_section(CALL_OFFER, "video", direction="both")


def test_extract_section_picks_the_first_active_match() -> None:
    offer = "\n".join(
        [
            "v=0",
            "a=group:BUNDLE a1 a2",
            "m=audio 0 RTP/AVP 0",
            "a=mid:a1",
            "m=audio 9 RTP/AVP 8",
            "a=mid:a2",
            "a=recvonly",
            "m=audio 9 RTP/AVP 0",
            "a=mid:a3",
        ]
    )
    single, _ = extract_section(offer, "audio")
    assert single == "v=0\na=group:BUNDLE a2\nm=audio 9 RTP/AVP 8\na=mid:a2\na=recvonly"
    single, _ = extract_section(offer, "audio", direction="sendrecv")
    assert single.endswith("a=mid:a3")
    assert "a=group" not in single


def test_extract_section_raises_when_the_kind_is_missing() -> None:
    with pytest.raises(ValueError, match="no active text section"):
        extract_section(CALL_OFFER, "text")
    with pytest.raises(ValueError, match="no active video section"):
        extract_section("v=0\nm=video 0 RTP/AVP 96\n", "video")


def test_extract_section_keeps_other_group_semantics_restricted() -> None:
    offer = "v=0\na=group:LS audio video\na=group:BUNDLE video\nm=audio 9 RTP/AVP 0\na=mid:audio\n"
    offer += "m=video 9 RTP/AVP 96\na=mid:video\n"
    single, _ = extract_section(offer, "audio")
    assert single.startswith("v=0\na=group:LS audio\nm=audio")


def test_restore_rejects_every_other_section() -> None:
    _, restore = extract_section(CALL_OFFER, "audio")
    answer = restore(AUDIO_ANSWER)
    lines = answer.split("\r\n")
    assert answer.endswith("\r\n")
    assert lines[:5] == ["v=0", "o=- 9 9 IN IP4 0.0.0.0", "s=-", "t=0 0", "a=group:BUNDLE audio"]
    assert lines[5:-1] == [
        "m=audio 40000 UDP/TLS/RTP/SAVPF 0",
        "c=IN IP4 192.0.2.20",
        "a=recvonly",
        "a=mid:audio",
        "a=rtpmap:0 PCMU/8000",
        "m=video 0 UDP/TLS/RTP/SAVPF 107",
        "c=IN IP4 0.0.0.0",
        "a=mid:video",
        "a=inactive",
        "m=application 0 UDP/DTLS/SCTP webrtc-datachannel",
        "c=IN IP4 0.0.0.0",
        "a=mid:data",
        "a=inactive",
    ]
    sections = media_sections(answer)
    assert [(s.kind, s.port, s.direction) for s in sections] == [
        ("audio", 40000, "recvonly"),
        ("video", 0, "inactive"),
        ("application", 0, "inactive"),
    ]
    assert [s.mid for s in sections] == [s.mid for s in media_sections(CALL_OFFER)]


def test_restore_keeps_the_original_position_of_the_section() -> None:
    _, restore = extract_section(CALL_OFFER, "video")
    answer = restore("v=0\nc=IN IP4 192.0.2.20\nm=video 9 UDP/TLS/RTP/SAVPF 107\na=mid:video\n")
    assert answer == "\n".join(
        [
            "v=0",
            "c=IN IP4 192.0.2.20",
            "m=audio 0 UDP/TLS/RTP/SAVPF 0",
            "a=mid:audio",
            "a=inactive",
            "m=video 9 UDP/TLS/RTP/SAVPF 107",
            "a=mid:video",
            "m=application 0 UDP/DTLS/SCTP webrtc-datachannel",
            "a=mid:data",
            "a=inactive",
            "",
        ]
    )


def test_restore_without_mids_or_formats() -> None:
    offer = "v=0\nm=audio 9 RTP/AVP 0\nm=video 9 RTP/AVP\n"
    _, restore = extract_section(offer, "audio")
    assert restore("v=0\nm=audio 9 RTP/AVP 0\na=recvonly") == (
        "v=0\nm=audio 9 RTP/AVP 0\na=recvonly\nm=video 0 RTP/AVP 0\nc=IN IP4 0.0.0.0\na=inactive"
    )


def test_restore_restricts_the_answer_bundle_group() -> None:
    _, restore = extract_section(CALL_OFFER, "audio")
    answer = restore(AUDIO_ANSWER.replace("BUNDLE audio", "BUNDLE audio video"))
    assert "a=group:BUNDLE audio\r\n" in answer
    answer = restore(AUDIO_ANSWER.replace("a=group:BUNDLE audio\r\n", ""))
    assert "a=group" not in answer


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ("v=0\n", "exactly one"),
        ("v=0\nm=video 9 RTP/AVP 96\n", "expected 'audio'"),
        ("v=0\nm=audio 9 RTP/AVP 0\nm=audio 9 RTP/AVP 0\n", "exactly one"),
    ],
)
def test_restore_rejects_unexpected_answers(answer: str, error: str) -> None:
    _, restore = extract_section(CALL_OFFER, "audio")
    with pytest.raises(ValueError, match=error):
        restore(answer)


def test_extract_and_normalize_compose() -> None:
    single, restore_section = extract_section(CALL_OFFER, "audio")
    normalized, restore_mids = normalize_mids(single)
    assert "a=mid:0" in normalized
    peer_answer = AUDIO_ANSWER.replace("BUNDLE audio", "BUNDLE 0").replace("mid:audio", "mid:0")
    answer = restore_section(restore_mids(peer_answer))
    assert [s.mid for s in media_sections(answer)] == ["audio", "video", "data"]
