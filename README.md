# aiphone-jo

> [!WARNING]
> **Unofficial project, not affiliated with or endorsed by Aiphone.** Door
> commands act on real hardware; use at your own risk.

Asyncio Python library for the **Aiphone JO-1MDW** video intercom. It covers:

- local pairing;
- the cloud connection (AWS IoT MQTT);
- the monitor's outputs (door release);
- ring detection and call answering;
- video signaling (Janus WebRTC relay).

It has **no dependency on Home Assistant**. It holds the whole protocol of the
[Home Assistant integration](https://github.com/gclem/hass-aiphone-jo), which
ships a copy of it, and can be used on its own or through its command line
tool.

## Status

- **Version 0.4**, approaching 1.0. Used every day through the Home Assistant
  integration on real JO-1MDW installations.
- **JO-1MDW only** (`HEADER.SYS 4`, `VER 1.2`). Other Aiphone models use
  different headers, regions, station identifiers and acceptance rules, and
  are not supported.
- **Releases:** published as GitHub Releases, not on PyPI.

## Features

| Feature | API | Status |
|---|---|---|
| LAN discovery (UDP 51711/51712) | `discover()` | Supported |
| Local pairing: pinned TLS, code, `/registClient`, terminal registration | `PairingFlow` | Supported |
| Room info: terminals, camera stations (`/getRoomInfo`) | `get_room_info()` | Supported |
| MQTT connection (mutual TLS), own topic and monitor-wide topic, automatic reconnection | `AiphoneJoClient.connect()` | Supported |
| Lock status (26031 → 27031) | `get_lock_status()` | Supported |
| Lock output 1, unlock (26021 → 27021) | `open_door(Output.UNLOCK, …)` | Supported |
| Lock output 2, option output (26121 → 27121) | `open_door(Output.OPTION, …)` | Supported |
| Monitoring session (30001 → 23021) and clean teardown (30031 + 23003), also when cancelled | `monitor()` | Supported |
| Station-side end of session (~60 s, 23003 → 30031 + 24003) | automatic | Supported |
| Video/audio signaling (Janus offer 31011, answer 30012), renewed sessions | `MonitorSession.request_offer()` / `send_answer()`, `live_monitor()` | Supported |
| Doorbell detection (23001), cancel (23004), end reasons | `RingEvent`, `CallUpdatedEvent` | Supported |
| History of monitoring sessions, closing those left open (also after a restart) | `session_history`, `restore_sessions()`, `SessionRecordEvent` | Supported |
| Video of a ringing call without answering it | `answer_call()` + `CallSession.withdraw()` | Supported |
| Answering a call (30011/30012, 24001, 30021, 24002 → 25001, 23008), declining (603) | `answer_call()` | Experimental |
| Snapshots and live video with aiortc (optional `media` extra) | `snapshot()`, `video_stream()` | Experimental |
| Unregistering a terminal (`/deleteClientInfo`) | `unregister()`, `PairingFlow.cancel()` | Experimental |
| Lock (26032) | `messages.build_lock_request()` (builder only) | Untested |

The monitoring sessions, both lock outputs, doorbell detection and the video
of a ringing call are verified on real JO-1MDW installations. Each output
command sends a pulse on the corresponding output of the monitor; what it
opens depends on how the installation is wired.

## Installation

Python 3.12 or later:

```bash
pip install "aiphone-jo @ git+https://github.com/gclem/aiphone-jo-lib@vX.Y.Z"
# with snapshots and live video through aiortc:
pip install "aiphone-jo[media] @ git+https://github.com/gclem/aiphone-jo-lib@vX.Y.Z"
```

## Quick start

Pair once, with the monitor in *add smartphone* mode:

```python
import aiohttp
from aiphone_jo import PairingFlow, discover

async with aiohttp.ClientSession() as session:
    monitor = (await discover())[0]
    async with PairingFlow(session) as flow:
        peer = await flow.connect(monitor)       # TLS handshake, nothing sent yet
        flow.approve(peer.fingerprint)           # after the user checked it
        await flow.request_code()                # the monitor displays a code
        await flow.authenticate(input("Code: "))
        await flow.register_cloud()              # /registClient
        credentials = await flow.register_terminal("Home")

store(credentials.to_storage())  # contains a private key: store it securely
```

Then connect to the cloud, follow the events and use the outputs:

```python
from aiphone_jo import AiphoneJoClient, CloudCredentials, Output

client = AiphoneJoClient(CloudCredentials.from_storage(stored), session=session)
client.add_listener(print)                          # RingEvent, CallUpdatedEvent, ...
await client.connect()                              # reconnects automatically
stations = (await client.get_room_info()).camera_terminals
await client.open_door(Output.UNLOCK, stations[0])  # pulse on output 1
await client.disconnect()
```

## Documentation

| Guide | Content |
|---|---|
| [Pairing](docs/pairing.md) | Pairing flow, errors and their reasons, stored credentials, unregistering |
| [Cloud client](docs/client.md) | Connection, outputs, monitoring sessions, errors, use in Home Assistant |
| [Video](docs/video.md) | `media` extra, `live_monitor()` for another WebRTC stack, SDP helpers |
| [Calls](docs/calls.md) | Rings, answering, picture of a ringing call, end reasons |
| [Protocol](docs/PROTOCOL.md) | Reference of the JO-1MDW messages |

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

## Known limitations

- **Door commands** are only honoured during a monitoring session. So
  `open_door()` opens a short one, sends the command and closes it.
- **The station ends each monitoring session** about 60 s after it starts.
  Live video therefore pauses briefly when the session is renewed.
- **One session at a time**: a station answers `486` (`StationBusyError`)
  while another session is open on it.
- **One MQTT connection at a time** per client certificate: never run two
  clients with the same paired identity.
- **Experimental features:** call answering, the `media` extra and
  `unregister()`. The monitor's own smartphone list is managed from the
  monitor.

## Security and privacy

- Logs are redacted (secrets and identifiers), and sensitive fields are
  excluded from `repr`.
- MQTT TLS is always verified, and the monitor's self-signed certificate is
  pinned by fingerprint.
- The CLI stores the paired identity in a file readable by its owner only
  (mode `0600`); protect it like a password.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```bash
uv sync
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run mypy
uv run pytest --cov
```

Tests never touch real hardware or the Aiphone cloud: they use loopback
UDP/TLS servers, a minimal MQTT broker and fake transports. Fixtures only
contain fictitious values. See [CONTRIBUTING.md](CONTRIBUTING.md) and the
[changelog](CHANGELOG.md).

## License

MIT, see [LICENSE](LICENSE).
