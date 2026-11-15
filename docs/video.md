# Video

[← README](../README.md)

The station sends its video through a Janus relay (WebRTC, ICE-lite). The
relay sends the offer and expects a complete, receive-only answer. The offer
contains H264 video (`profile-level-id=42e01f`, `packetization-mode=1`) and
PCMU audio.

There are two ways to receive the video:

- **The `media` extra**, which does everything with aiortc. It is
  experimental.
- **The low-level `live_monitor()` API**, with another WebRTC stack, for
  example a media server such as go2rtc. The Home Assistant integration uses
  this one.

## `media` extra (experimental)

```bash
pip install "aiphone-jo[media] @ git+https://github.com/gclem/aiphone-jo-lib@vX.Y.Z"
```

```python
client = AiphoneJoClient(credentials, session=session, camera_terminal=station)

jpeg = await client.snapshot()                    # one JPEG, session closed afterwards

async with client.video_stream() as stream:       # live view, renewed every ~60 s
    async for frame in stream.frames():           # av.VideoFrame objects
        ...
    jpeg = stream.latest_jpeg(quality=80)         # most recent frame, or None
```

- **`snapshot(terminal=None, *, timeout=15.0)`:**
  - opens a monitoring session, negotiates WebRTC with aiortc, decodes the
    first video frame and always closes the session;
  - while a `video_stream()` is open, it returns the stream's latest frame
    instead.
- **`video_stream(terminal=None)`** keeps the view alive:
  - when the station ends the session (after about 60 s), a new one is
    negotiated automatically, with a gap of a few seconds;
  - while it is open, `open_door()` reuses its session.
- **With `terminal=None`**, the client uses `camera_terminal`, or else the
  first camera station from `/getRoomInfo` that is not busy.
- **Errors:** `MediaUnavailableError` (extra not installed),
  `MediaTimeoutError`, `StationBusyError` (486).

The extra depends on aiortc, which does not pin `av` in this library. Home
Assistant 2026.10 pins `av==19.0.0`, which requires an aiortc release newer
than 1.15.0 (aiortc 1.15.0 accepts `av>=14,<18`).

## Another WebRTC stack: `live_monitor()`

The core never imports aiortc. Another WebRTC stack can reuse the same session
handling through `live_monitor()`:

- it renews the session when the station ends it;
- it calls an `async (offer_sdp) -> answer_sdp` callback for every new
  session.

```python
async def negotiate(offer_sdp: str) -> str:
    ...  # apply the relay's offer, return a complete answer (no trickle ICE)

async with client.live_monitor(negotiate, station, on_session_end=cleanup) as live:
    await live.wait_stopped()
```

## SDP helpers

The relay names its media sections `audio` and `video` and advertises the
`sdes:mid` RTP header extension. Some WebRTC servers (for example go2rtc)
expect numeric mids and answer `inactive` otherwise.

`aiphone_jo.sdp.normalize_mids(offer)` returns two things:

- the offer with numeric mids and without that extension;
- a `restore(answer)` function that puts the original mids back before the
  answer is sent to the relay.

```python
from aiphone_jo.sdp import normalize_mids

async def negotiate(offer_sdp: str) -> str:
    offer, restore = normalize_mids(offer_sdp)
    answer = await other_webrtc_server.answer(offer)
    return restore(answer)
```

An offer can also be reduced to a single media section, for a peer that only
handles one of them. `restore(answer)` then rebuilds the complete answer, with
the other sections rejected (port 0, `a=inactive`, removed from `BUNDLE`):

```python
from aiphone_jo.sdp import extract_section

audio_offer, restore = extract_section(offer_sdp, "audio", direction="sendrecv")
answer_sdp = restore(await audio_only_peer.answer(audio_offer))
```

`media_sections(sdp)` describes the sections (kind, port, mid, direction...).
