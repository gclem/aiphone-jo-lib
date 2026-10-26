# Changelog

All notable changes to `aiphone-jo` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added

- Optional video support, `media` extra (aiortc and Pillow, experimental):
  `AiphoneJoClient.snapshot()` (JPEG) and `AiphoneJoClient.video_stream()`
  (`frames()`, `latest_jpeg()`, automatic session renewal), with
  `MediaUnavailableError` and `MediaTimeoutError`.
- `AiphoneJoClient.live_monitor()` / `LiveMonitor`: monitoring session renewed
  automatically, with an `async (offer_sdp) -> answer_sdp` negotiation callback
  and no media dependency.
- `AiphoneJoClient(camera_terminal=...)` and `start_camera_session()`: default
  camera station, or the first non-busy one from `/getRoomInfo`.

## [0.2.2] - 2026-10-26

### Added

- `Output` enum (`Output.UNLOCK` = lock output 1, `Output.OPTION` = lock
  output 2) with `Output.number`, `Output.from_number()` and `Output.coerce()`;
  `open_door()` and `RelaySession.open()` also accept `1` and `2`.
- CLI: `aiphone-jo open --output 1|2`.
- Both lock outputs and the session teardown are verified on a JO-1MDW.

### Changed

- Lock outputs are described as output 1 (unlock) and output 2 (option
  output); their effect depends on the installation. The documentation no
  longer refers to "large" or "small" openings.
- `open_door()` and `RelaySession.open()` take `output` as first argument.

### Deprecated

- `Gate` (alias of `Output`; `Gate.MAIN` is `Output.UNLOCK`), the `gate=`
  keyword of `open_door()` / `RelaySession.open()`, and the CLI options
  `open --big` / `open --small` (hidden; use `--output 1` / `--output 2`).

## [0.2.1] - 2026-10-26

### Added

- Protocol reference in `docs/PROTOCOL.md`, `ROADMAP.md` and `CONTRIBUTING.md`.

### Changed

- The default terminal display name sent with `monitor_req` (when no terminal
  name is known) is now `aiphone-jo`.
- `Terminal.ent_key_type_label` returns `lockable` / `unlockable`.
- Documentation and docstrings rewritten to describe the protocol behaviour.

### Removed

- The undocumented `aiphone_jo.mid` module (it was not part of the public API).

## [0.2.0] - 2026-10-26

### Added

- `unregister()` (`/deleteClientInfo`, idempotent) and `PairingFlow.cancel()`
  (experimental).
- Typed pairing failures: `PairingStage`, stable `PairingFailureReason`
  values, `mid`, `rslt`, `hint` and `retry_same_connection` on every
  `PairingError`; `IncompatibleVersionError` (70002), `PairingLinkClosedError`,
  `PairingTimeoutError`, `PairingCloudError` (71001); public tables
  `PAIRING_RSLT_REASONS`, `PAIRING_HINTS`, `RETRY_ON_SAME_CONNECTION`.
- `PairingFlow.reconnect()`, retry of a wrong code or rejected name on the same
  connection, `register_terminal(name, auto_rename=...)`, `approved_fingerprint`.
- Discovery reports 11001 refusals (`DiscoveredMonitor.rslt`,
  `include_refused=True`, or `PairingRejectedError`).
- `CloudCredentials.to_storage()` / `from_storage()` (`schema`, migration of
  0.1.0 data, unknown keys ignored), `is_expired()`, `monitor_id`, and
  `CredentialsExpiredError`.
- `CloudAuthError` when the cloud refuses the identity; automatic reconnection
  stops and `ConnectionStateEvent.auth_failed` is set.
- `aiphone-jo unregister` command.

### Changed

- `CloudCredentials.expires` is always set; `CloudApiError` has a `kind`.
- `/getRoomInfo` HTTP 401/403 raise `CloudAuthError`.
- `DiscoveryError` is now a `PairingError`.

### Deprecated

- `CloudCredentials.as_dict()`: use `to_storage()`.

## [0.1.0] - 2026-10-26

### Added

- LAN discovery, pinned-TLS local pairing (`PairingFlow`), `/registClient`,
  `/getRoomInfo`.
- `CloudCredentials` validation and serialization, non-blocking SSL context.
- MQTT client with automatic reconnection and monitor-wide topic subscription.
- Door release (`open_door`, lock outputs 1 and 2), lock status,
  monitoring sessions with Janus SDP signaling, automatic answer to the
  station-side end of session.
- Doorbell tracking and call answering (experimental).
- Log redaction and the `aiphone-jo` command line tool.
