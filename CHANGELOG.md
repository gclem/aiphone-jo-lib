# Changelog

All notable changes to `aiphone-jo` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

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
- Door release (`open_door`, large and small openings), lock status,
  monitoring sessions with Janus SDP signaling, automatic answer to the
  station-side end of session.
- Doorbell tracking and call answering (experimental).
- Log redaction and the `aiphone-jo` command line tool.
