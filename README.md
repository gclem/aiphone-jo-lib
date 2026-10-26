# aiphone-jo

Unofficial asyncio Python library for the **Aiphone JO-1MDW** video intercom:
local pairing, cloud (AWS IoT MQTT) connection, door release, lock status, ring
detection and video signaling (Janus WebRTC relay).

It has **no dependency on Home Assistant** and is designed to be used by a
Home Assistant integration.

> [!WARNING]
> **Unofficial project, not affiliated with or endorsed by Aiphone.** It
> targets the **JO-1MDW only** (`HEADER.SYS 4`, `VER 1.2`): other Aiphone
> models use different headers, regions, station identifiers and acceptance
> rules, and are **not** supported. Door commands act on real hardware; use at
> your own risk.

## Features

| Feature | API | Status |
|---|---|---|
| LAN discovery (UDP 51711/51712) | `discover()` | Supported |
| Local pairing: pinned TLS, code, `/registClient`, terminal registration | `PairingFlow` | Supported |
| Room info: terminals, camera stations (`/getRoomInfo`) | `get_room_info()` | Supported |
| MQTT connection (mutual TLS), own topic + monitor-wide topic | `AiphoneJoClient.connect()` | Supported |
| Lock status (26031 → 27031) | `get_lock_status()` | Supported |
| Lock output 1, unlock (26021 → 27021) | `open_door(Output.UNLOCK, …)` | Supported |
| Lock output 2, option output (26121 → 27121) | `open_door(Output.OPTION, …)` | Supported |
| Monitoring session (30001 → 23021) and clean teardown (30031 + 23003) | `monitor()` | Supported |
| Station-side end of session (~60 s, 23003 → 30031 + 24003) | automatic | Supported |
| Video/audio signaling (Janus offer 31011, answer 30012) | `MonitorSession.request_offer()` / `send_answer()`, `live_monitor()` | Supported |
| Snapshots and live video (optional `media` extra, aiortc) | `snapshot()`, `video_stream()` | Experimental |
| Doorbell detection (23001), cancel (23004) | `RingEvent`, `CallUpdatedEvent` | Experimental |
| Answering a call (24001/30021/24002/23008), declining (603) | `answer_call()` | Experimental |
| Unregistering a terminal (`/deleteClientInfo`) | `unregister()`, `PairingFlow.cancel()` | Experimental |
| Lock (26032) | `messages.build_lock_request()` (builder only) | Untested |

Both lock outputs and the session teardown (client side and station side) are
verified on a JO-1MDW. Each output command sends a pulse on the corresponding
output of the monitor; what it opens depends on how the installation is wired.

## Known limitations

- Doorbell detection and call answering are experimental and may not work on
  every installation.
- `unregister()` is experimental; the monitor's own smartphone list is managed
  from the monitor.
- The door commands (`unlock`, `out_option`) are only honoured **during an
  active monitoring session**; outside a session the station silently ignores
  them. `open_door()` therefore opens a short monitoring session, sends the
  command and always closes the session (`LEAVE_USER_REQ` 30031 then
  `DISCNCT_REQ` 23003).
- The station closes a monitoring session by itself about **60 s** after
  `monitor_req`; the client answers automatically, otherwise the next
  `monitor_req` gets `RSLT 486`. Live video therefore stops after about 60 s.
- A site can have several camera stations; one may answer `486` (busy) while
  another answers `200`. The library exposes all of them.
- **A client certificate can only hold one MQTT connection at a time.** Never
  run two clients with the same paired identity.
- Video (`media` extra) is experimental. It depends on aiortc, which does
  not pin `av` in this library; Home Assistant 2026.10 pins `av==19.0.0`, which
  requires an aiortc release newer than 1.15.0 (aiortc 1.15.0 accepts
  `av>=14,<18`).
- Live video is renewed every ~60 s by the station; expect a short gap.

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the protocol reference and
[ROADMAP.md](ROADMAP.md) for planned features.

## Installation

Python 3.12 or later. The library is not published on PyPI; releases are
published as GitHub Releases. For development or the CLI:

```bash
pip install "aiphone-jo @ git+https://github.com/gclem/aiphone-jo-lib@vX.Y.Z"
```

## Usage

### Pairing (once)

Put the monitor in *add smartphone* mode, then:

```python
import aiohttp
from aiphone_jo import PairingError, PairingFlow, discover

async with aiohttp.ClientSession() as session:
    monitor = (await discover())[0]
    async with PairingFlow(session) as flow:
        peer = await flow.connect(monitor)       # TLS handshake, nothing sent yet
        print(peer.fingerprint)                  # show it to the user
        flow.approve(peer.fingerprint)           # after the user confirmed it
        await flow.request_code()                # the monitor displays a code
        await flow.authenticate(input("Code: "))
        await flow.register_cloud()              # /registClient
        credentials = await flow.register_terminal("Home")

store(credentials.to_storage())  # contains a private key: store it securely
```

Every pairing failure is a `PairingError` with fields meant for a user
interface:

| Field | Content |
|---|---|
| `stage` | `PairingStage`: `discovery`, `compatibility`, `tls`, `trust`, `code_request`, `authentication`, `cloud_registration`, `terminal_registration` |
| `reason` | `PairingFailureReason`, a stable string (see below) |
| `mid`, `rslt` | reply MID (`11001`, `11003`, `11005`...) or application error code (`70002`, `71001`), and the raw result code |
| `hint` | short English explanation |
| `retry_same_connection` | the same step can be called again without reconnecting |

| `mid` | `rslt` | `reason` |
|---|---|---|
| `11001` | 1 / 2 / 3 | `retry` / `registration_in_progress` / `terminal_limit_reached` |
| `11003` | 1 or 2 / 3 | `wrong_code` / `code_expired` |
| `11005` | 1 / 2 / 3 / 4 | `retry` / `name_too_long` / `name_already_used` / `eula_not_accepted` |
| `70002` | 1 | `incompatible_version` (unsupported monitor system type) |
| `71001` | HTTP status | `/registClient` failure (451: `eula_not_accepted`) |

Other reasons: `monitor_not_found`, `tls_failed`, `link_closed` (the monitor
closed the connection, for example when the pairing window expired),
`fingerprint_mismatch`, `timeout`, `cloud_http_error`, `cloud_network_error`,
`cloud_invalid_response`, `unknown_result`, `invalid_state`. The tables are
available as `PAIRING_RSLT_REASONS`, `PAIRING_HINTS` and `pairing_reason()`.

The flow stays on the same connection after a wrong code (11003 RSLT 2) or a rejected name (11005 RSLT 2 or 3,
`register_terminal(name, auto_rename=False)`); call the step again. Other
failures close the connection: `await flow.reconnect()` reopens it with the
already approved fingerprint (the monitor must still be in *add smartphone*
mode). `await flow.cancel()` aborts and deletes a cloud registration that has
no terminal yet. `discover()` reports monitors that refuse the request
(11001) as `PairingRejectedError`, or returns them with `rslt` set when
`include_refused=True`.

`flow.monitor_id` (also `CloudCredentials.monitor_id`), available after
`register_cloud()`, is a stable, non-secret identifier of the monitor that
survives re-pairing, suitable as a unique id. It is a truncated hash, never the
MAC address in clear.

### Stored credentials

```python
from aiphone_jo import CloudCredentials, CredentialsExpiredError

data = credentials.to_storage()           # {"schema": 1, ..., "expires": "..."}
credentials = CloudCredentials.from_storage(data)
credentials.expires, credentials.is_expired()
```

`from_storage()` migrates older layouts (including 0.1.0 `as_dict()` data,
now deprecated), ignores unknown keys, rejects data from a newer schema with
`InvalidCredentialsError`, and raises `CredentialsExpiredError` for an expired
client certificate. Client certificates are typically valid for about 50
years; to renew one, pair again.

`await unregister(session, credentials)` deletes the terminal from the Aiphone
cloud (`/deleteClientInfo`). It is idempotent: it returns `False` when the
terminal was already deleted. It is **experimental**. The monitor's own
smartphone list is managed from the monitor.

### Cloud client

```python
from aiphone_jo import AiphoneJoClient, CloudCredentials, Output, RingEvent

credentials = CloudCredentials.from_storage(stored_dict)
client = AiphoneJoClient(credentials, session=session)
client.add_listener(lambda event: print(event))  # RingEvent, CallUpdatedEvent, ...
await client.connect()                            # reconnects automatically

status = await client.get_lock_status()
stations = (await client.get_room_info()).camera_terminals

await client.open_door(Output.UNLOCK, stations[0])  # lock output 1 (or: 1)
await client.open_door(Output.OPTION, stations[0])  # lock output 2 (or: 2)

async with client.monitor(stations[0]) as live:   # real monitoring call
    offer = await live.request_offer()            # Janus SDP offer
    await live.send_answer(answer_from_your_webrtc_stack(offer))
    await live.unlock()                           # reuses this session

await client.disconnect()
```

If the cloud refuses the identity (revoked or deleted terminal: MQTT "not
authorized", client certificate rejected, own topic refused, HTTP 401/403),
`CloudAuthError` is raised (or, while connected, a `ConnectionStateEvent`
with `auth_failed=True` is emitted) and automatic reconnection stops: pair
again.

### Video (optional `media` extra)

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

- `snapshot(terminal=None, *, timeout=15.0)` opens a monitoring session,
  negotiates WebRTC with aiortc, decodes the first video frame and always
  closes the session. While a `video_stream()` is open it returns the stream's
  latest frame instead.
- `video_stream(terminal=None)` keeps the view alive: when the station ends the
  session (after about 60 s) a new one is negotiated automatically, with a gap
  of a few seconds. While it is open, `open_door()` reuses its session.
- With `terminal=None`, the client uses `camera_terminal`, or else the first
  camera station from `/getRoomInfo` that is not busy.
- Errors: `MediaUnavailableError` (extra not installed), `MediaTimeoutError`,
  `StationBusyError` (486).

The core never imports aiortc. Another WebRTC stack (for example a bridge to a
media server) can reuse the same session handling through the low-level
`live_monitor()` API, which renews the session and calls an
`async (offer_sdp) -> answer_sdp` callback for every new session:

```python
async def negotiate(offer_sdp: str) -> str:
    ...  # apply the relay's offer, return a complete answer (no trickle ICE)

async with client.live_monitor(negotiate, station, on_session_end=cleanup) as live:
    await live.wait_stopped()
```

The relay is a Janus gateway (ICE-lite): it sends the offer (H264 video with
`profile-level-id=42e01f`, `packetization-mode=1`; PCMU audio) and expects a
complete, receive-only answer.

### Home Assistant usage

- **Config flow**: `discover()` then `PairingFlow` (the TLS connection stays
  open between steps), then the choice of the camera station from
  `camera_terminals`; store `CloudCredentials.to_storage()` in the entry and
  use `monitor_id` as the unique id. Map `PairingError.reason` to UI errors.
- **Coordinator**: push-based, `add_listener()` + `connect()`;
  `ConnectionStateEvent` drives availability. `CloudAuthError`,
  `ConnectionStateEvent(auth_failed=True)` and `CredentialsExpiredError` map
  to re-authentication.
- **Entities**: ring event / binary sensor (`RingEvent`, `CallUpdatedEvent`),
  one button per lock output (`open_door`), lock status sensor, and a camera
  using `snapshot()` for still images and `video_stream()` or
  `live_monitor()` for live view.
- Use `CloudCredentials.async_create_ssl_context()` (already done by
  `connect()`) to avoid blocking the event loop.

## Command line tool

Commands acting on the hardware ask for confirmation (`--yes` skips it).

```bash
aiphone-jo discover                          # find monitors in "add smartphone" mode
aiphone-jo pair --name Home                  # pair and save the identity (mode 0600)
aiphone-jo rooms                             # list terminals and camera stations
aiphone-jo status                            # lock status
aiphone-jo open --output 1 --terminal <id>   # lock output 1 (unlock)
aiphone-jo open --output 2 --terminal <id>   # lock output 2 (option output)
aiphone-jo ring-watch                        # print rings and events
aiphone-jo unregister                        # delete the terminal from the cloud
```

The identity file defaults to `~/.config/aiphone-jo/credentials.json`
(`--credentials` to change it).

## Errors

All exceptions derive from `AiphoneJoError`:

- `PairingError`, with `DiscoveryError` (`NoMonitorFoundError`),
  `UntrustedPeerError`, `PairingRejectedError`, `IncompatibleVersionError`,
  `PairingLinkClosedError`, `PairingTimeoutError` and `PairingCloudError`;
- `CloudApiError` (`status`, `kind`: `http`, `network` or `invalid_response`);
- `CloudConnectionError`, with `CloudAuthError` (identity refused);
- `InvalidCredentialsError`, with `CredentialsExpiredError`;
- `NotConnectedError`, `ResponseTimeoutError`, `CommandRejectedError`
  (`StationBusyError` for `RSLT 486`) and `SessionError`.

## Security and privacy

Logs are redacted (secrets and identifiers), sensitive fields are excluded from
`repr`, MQTT TLS is always verified, and the monitor's self-signed certificate
is pinned by fingerprint. The CLI stores the paired identity in a file
readable by its owner only (mode `0600`); protect it like a password. To report
a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```bash
uv sync
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run mypy
uv run pytest --cov
```

Tests never touch real hardware or the Aiphone cloud: they use loopback
UDP/TLS servers, a minimal MQTT broker and fake transports. Fixtures only
contain fictitious values. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE).
