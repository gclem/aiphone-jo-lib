"""Video support: full WebRTC negotiation on the loopback interface with a fake relay."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("aiortc")

import av
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.mediastreams import AudioStreamTrack, VideoStreamTrack

from aiphone_jo import (
    AiphoneJoClient,
    CloudCredentials,
    MediaTimeoutError,
    Output,
    StationBusyError,
    Terminal,
)
from aiphone_jo.media import VideoStream, encode_jpeg

from .conftest import FakeBroker, default_responder, reply_to

STATION = Terminal(id=102, term_kind=5, name="Entrance", camera=True, ent_key_type=2)
JPEG_MAGIC = b"\xff\xd8\xff"


class FakeRelay:
    """Plays the Janus relay: makes the offer on 30011, applies the answer on 30012."""

    def __init__(self, *, video: bool = True) -> None:
        self.video = video
        self.peers: dict[str, RTCPeerConnection] = {}
        self.offers: list[str] = []
        self.answers: list[str] = []
        self.busy = False

    async def __call__(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        header = message["HEADER"]
        mid, cid = header["MID"], header.get("CID", "")
        if mid == "23021" and self.busy:
            return [reply_to(message, "24021", rslt=486)]
        if mid == "30011":
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.peers[cid] = pc
            if self.video:
                transceiver = pc.addTransceiver(VideoStreamTrack(), direction="sendonly")
                h264 = [
                    codec
                    for codec in RTCRtpSender.getCapabilities("video").codecs
                    if codec.mimeType == "video/H264"
                ]
                transceiver.setCodecPreferences(h264)
            pc.addTransceiver(AudioStreamTrack(), direction="sendrecv")
            await pc.setLocalDescription(await pc.createOffer())
            self.offers.append(pc.localDescription.sdp)
            return [reply_to(message, "31011", SDP=pc.localDescription.sdp)]
        if mid == "30012":
            answer = message["BODY"]["SDP"]
            self.answers.append(answer)
            await self.peers[cid].setRemoteDescription(
                RTCSessionDescription(sdp=answer, type="answer")
            )
            return [reply_to(message, "31012")]
        if mid == "30031" and cid in self.peers:
            await self.peers.pop(cid).close()
        return default_responder(message)

    async def close(self) -> None:
        for pc in self.peers.values():
            await pc.close()


@pytest.fixture
async def relay(broker: FakeBroker) -> AsyncIterator[FakeRelay]:
    fake = FakeRelay()
    broker.responder = fake
    yield fake
    await fake.close()


@pytest.fixture
async def client(
    credentials: CloudCredentials, broker: FakeBroker, relay: FakeRelay
) -> AsyncIterator[AiphoneJoClient]:
    instance = AiphoneJoClient(credentials, transport_factory=broker, camera_terminal=STATION)
    await instance.connect()
    yield instance
    await instance.disconnect()


def media_lines(sdp: str) -> list[str]:
    return [line for line in sdp.splitlines() if line.startswith("m=")]


async def test_snapshot_negotiates_and_closes(
    client: AiphoneJoClient, broker: FakeBroker, relay: FakeRelay
) -> None:
    image = await client.snapshot(timeout=20)
    assert image.startswith(JPEG_MAGIC)
    assert broker.connection.mids == ["30001", "23021", "30011", "30012", "30031", "23003"]
    assert "H264" in relay.offers[0]
    answer = relay.answers[0]
    # Complete, receive-only answer: candidates included, no STUN-derived ones.
    assert len(media_lines(answer)) == 2
    assert answer.count("a=recvonly") == 2
    assert "a=sendrecv" not in answer
    assert re.search(r"a=rtpmap:\d+ H264/90000", answer)
    assert "a=candidate:" in answer
    assert " typ srflx" not in answer
    assert client.active_session is None


async def test_snapshot_timeout_still_closes_the_session(
    client: AiphoneJoClient, broker: FakeBroker, relay: FakeRelay
) -> None:
    relay.video = False
    with pytest.raises(MediaTimeoutError):
        await client.snapshot(timeout=2)
    assert broker.connection.mids[-2:] == ["30031", "23003"]
    assert client.active_session is None


async def test_snapshot_reports_a_busy_station(
    client: AiphoneJoClient, broker: FakeBroker, relay: FakeRelay
) -> None:
    relay.busy = True
    with pytest.raises(StationBusyError):
        await client.snapshot(timeout=5)
    assert "30011" not in broker.connection.mids


async def test_video_stream_renews_and_shares_its_session(
    client: AiphoneJoClient, broker: FakeBroker, relay: FakeRelay
) -> None:
    async with client.video_stream() as stream:
        assert isinstance(stream, VideoStream)
        assert stream.latest_jpeg() is None or stream.latest_jpeg() is not None
        frames = stream.frames()
        frame = await asyncio.wait_for(anext(frames), 20)
        assert isinstance(frame, av.VideoFrame)
        jpeg = stream.latest_jpeg(quality=60)
        assert jpeg is not None
        assert jpeg.startswith(JPEG_MAGIC)

        published = len(broker.connection.published)
        # snapshot() and open_door() reuse the stream's session.
        assert (await client.snapshot()).startswith(JPEG_MAGIC)
        await client.open_door(Output.UNLOCK)
        assert broker.connection.mids[published:] == ["26021"]

        # The station ends the session after ~60 s: the stream renews it.
        session = client.active_session
        assert session is not None
        broker.connection.inject({"HEADER": {"MID": "23003", "CID": session.cid, "REASON": 0}})
        async with asyncio.timeout(20):
            while stream.sessions < 2:
                await asyncio.sleep(0.05)
        frame = await asyncio.wait_for(anext(frames), 20)
        assert isinstance(frame, av.VideoFrame)
        await frames.aclose()
    mids = broker.connection.mids
    assert mids.count("30001") == 2
    assert mids.count("30012") == 2
    assert mids[-2:] == ["30031", "23003"]
    assert client.active_session is None
    assert not stream.running


async def test_video_stream_watchdog_renews_without_video(
    client: AiphoneJoClient, broker: FakeBroker, relay: FakeRelay
) -> None:
    relay.video = False
    async with VideoStream(client, STATION, frame_timeout=0.5) as stream:
        async with asyncio.timeout(10):
            while stream.sessions < 2:
                await asyncio.sleep(0.05)
        assert stream.latest_jpeg() is None
        with pytest.raises(MediaTimeoutError):
            await stream.wait_jpeg(timeout=0.1)


async def test_video_stream_start_failure(
    client: AiphoneJoClient, broker: FakeBroker, relay: FakeRelay
) -> None:
    relay.busy = True
    with pytest.raises(StationBusyError):
        async with client.video_stream():
            pass
    assert client._video_streams == []


def test_encode_jpeg_quality_bounds() -> None:
    frame = av.VideoFrame(width=16, height=16, format="yuv420p")
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    assert encode_jpeg(frame, 50).startswith(JPEG_MAGIC)
    for quality in (0, 101):
        with pytest.raises(ValueError, match="quality"):
            encode_jpeg(frame, quality)
