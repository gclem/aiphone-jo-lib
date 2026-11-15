# Pairing

[← README](../README.md)

## Pairing flow

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

**Steps that can be called again on the same connection:**

- after a wrong code (11003 RSLT 2);
- after a rejected name (11005 RSLT 2 or 3), with
  `register_terminal(name, auto_rename=False)`.

**Other failures close the connection:**

- `await flow.reconnect()` reopens it with the already approved fingerprint.
  The monitor must still be in *add smartphone* mode.
- `await flow.cancel()` aborts, and deletes a cloud registration that has no
  terminal yet.

**Monitors that refuse the discovery request** (11001): `discover()` reports
them as `PairingRejectedError`, or returns them with `rslt` set when
`include_refused=True`.

`flow.monitor_id` (also `CloudCredentials.monitor_id`) is available after
`register_cloud()`. It is a stable, non-secret identifier of the monitor that
survives re-pairing, suitable as a unique id. It is a truncated hash, never
the MAC address in clear.

## Pairing errors

Every pairing failure is a `PairingError` with fields meant for a user
interface:

| Field | Content |
|---|---|
| `stage` | `PairingStage`: `discovery`, `compatibility`, `tls`, `trust`, `code_request`, `authentication`, `cloud_registration`, `terminal_registration` |
| `reason` | `PairingFailureReason`, a stable string (see below) |
| `mid`, `rslt` | Reply MID (`11001`, `11003`, `11005`...) or application error code (`70002`, `71001`), and the raw result code |
| `hint` | Short English explanation |
| `retry_same_connection` | The same step can be called again without reconnecting |

| `mid` | `rslt` | `reason` |
|---|---|---|
| `11001` | 1 / 2 / 3 | `retry` / `registration_in_progress` / `terminal_limit_reached` |
| `11003` | 1 or 2 / 3 | `wrong_code` / `code_expired` |
| `11005` | 1 / 2 / 3 / 4 | `retry` / `name_too_long` / `name_already_used` / `eula_not_accepted` |
| `70002` | 1 | `incompatible_version` (unsupported monitor system type) |
| `71001` | HTTP status | `/registClient` failure (451: `eula_not_accepted`) |

The other reasons are:

- `monitor_not_found`;
- `tls_failed`;
- `link_closed`: the monitor closed the connection, for example when the
  pairing window expired;
- `fingerprint_mismatch`;
- `timeout`;
- `cloud_http_error`, `cloud_network_error`, `cloud_invalid_response`;
- `unknown_result`, `invalid_state`.

The tables are available as `PAIRING_RSLT_REASONS`, `PAIRING_HINTS` and
`pairing_reason()`.

## Stored credentials

```python
from aiphone_jo import CloudCredentials, CredentialsExpiredError

data = credentials.to_storage()           # {"schema": 1, ..., "expires": "..."}
credentials = CloudCredentials.from_storage(data)
credentials.expires, credentials.is_expired()
```

`from_storage()`:

- migrates older layouts, including the 0.1.0 `as_dict()` data (now
  deprecated);
- ignores unknown keys;
- rejects data from a newer schema with `InvalidCredentialsError`;
- raises `CredentialsExpiredError` for an expired client certificate.

Client certificates are typically valid for about 50 years; to renew one, pair
again.

## Unregistering

`await unregister(session, credentials)` deletes the terminal from the Aiphone
cloud (`/deleteClientInfo`). It is idempotent: it returns `False` when the
terminal was already deleted.

It is **experimental**. The monitor's own smartphone list is managed from the
monitor.
