# Cloud client

[← README](../README.md)

## Connection, outputs and monitoring

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

`client.connect()` subscribes to the terminal's own topic and to the
monitor-wide topic that carries the rings. A `ConnectionStateEvent` reports
every connection change.

## Monitoring sessions

- **Door commands** (`unlock`, `out_option`) are sent **during an active
  monitoring session**, where they are known to work. Whether the monitor
  honours them outside a session is not settled (see the protocol
  reference). So `open_door()` opens a short monitoring session, sends the
  command and always closes the session (`LEAVE_USER_REQ` 30031 then
  `DISCNCT_REQ` 23003).
- **The station ends a session** by itself about **60 s** after `monitor_req`.
  The client answers automatically; otherwise the next `monitor_req` gets
  `RSLT 486`.
- **A session cancelled while the station answers the request** (for example
  by a caller giving up after a timeout) is fully closed as well, so the
  station does not stay busy:
  - if the station's reply arrives later, the session it started is closed;
  - if the station ends such a session (`23003`), the client answers it.
    Unanswered, the station refuses every new session (486);
  - if the station answers busy (486) later, such sessions are closed again,
    once each.
- **The station's answer to a monitoring request** is awaited up to 30 s
  (`monitor_reply_timeout`), longer than other requests: giving up early
  is what can leave a session open on the station.
- **Several camera stations:** one may answer `486` (busy) while another
  answers `200`. The library exposes all of them.
- **One MQTT connection at a time:** a client certificate can only hold one
  MQTT connection. Never run two clients with the same paired identity.

## Identity refused

If the cloud refuses the identity, automatic reconnection stops: pair again.
This happens with a revoked or deleted terminal: MQTT "not authorized",
client certificate rejected, own topic refused, or HTTP 401/403.

- **Before the connection:** `CloudAuthError` is raised.
- **While connected:** a `ConnectionStateEvent` with `auth_failed=True` is
  emitted.

## Errors

All exceptions derive from `AiphoneJoError`:

- `PairingError`, with `DiscoveryError` (`NoMonitorFoundError`),
  `UntrustedPeerError`, `PairingRejectedError`, `IncompatibleVersionError`,
  `PairingLinkClosedError`, `PairingTimeoutError` and `PairingCloudError`;
- `CloudApiError` (`status`, `kind`: `http`, `network` or
  `invalid_response`);
- `CloudConnectionError`, with `CloudAuthError` (identity refused);
- `InvalidCredentialsError`, with `CredentialsExpiredError`;
- `NotConnectedError`, `ResponseTimeoutError`, `CommandRejectedError`
  (`StationBusyError` for `RSLT 486`) and `SessionError`, with
  `CallEndedError` (`reason`) and `CallAnsweredElsewhereError`.

## Use in Home Assistant

- **Config flow:**
  - `discover()` then `PairingFlow`; the TLS connection stays open between
    steps;
  - then the choice of the camera station from `camera_terminals`;
  - store `CloudCredentials.to_storage()` in the entry and use `monitor_id`
    as the unique id;
  - map `PairingError.reason` to UI errors.
- **Coordinator:**
  - push-based, with `add_listener()` and `connect()`;
  - `ConnectionStateEvent` drives availability;
  - `CloudAuthError`, `ConnectionStateEvent(auth_failed=True)` and
    `CredentialsExpiredError` map to re-authentication.
- **Entities:**
  - a ring event and binary sensor, from `RingEvent`, `CallUpdatedEvent` and
    its `reason`;
  - one button per lock output, with `open_door()`;
  - a camera, using `live_monitor()` with a WebRTC server (see
    [Video](video.md)).
- **Event loop:** use `CloudCredentials.async_create_ssl_context()` (already
  done by `connect()`) to avoid blocking it.

The [Home Assistant integration](https://github.com/gclem/hass-aiphone-jo)
follows this design.
