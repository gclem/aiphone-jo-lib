"""Pure SDP helpers (aiphone_jo.sdp)."""

from __future__ import annotations

from aiphone_jo.sdp import SDES_MID_EXTENSION, normalize_mids

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
