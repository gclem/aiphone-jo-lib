# JO-1MDW protocol reference

Technical reference of the messages used by `aiphone-jo`. It describes the
JO-1MDW (`HEADER.SYS 4`, `VER 1.2`) only.

## Overview

| Channel | Transport | Used for |
|---|---|---|
| LAN discovery | UDP broadcast, request to port 51711, replies on 51712 | finding a monitor in "add smartphone" mode |
| Local pairing | TLS 1.2 to the port announced by the monitor, JSON lines | pairing code, terminal registration |
| Cloud API | HTTPS `POST` with a JSON body, `https://<api origin>/<path>` | identity issuance, room info, unregistration |
| Cloud messaging | MQTT 3.1.1 over mutual TLS (AWS IoT), QoS 0 | control, monitoring, relay, ring |

All messages are JSON objects with a `HEADER` (always carrying `MID`, a
5-digit string) and, depending on the message, a `BODY`. Numbers sent in
`HEADER.SYS` and most identifiers are strings. `SID` is a timestamp
`YYYYMMDDhhmmss` followed by 6 digits of microseconds.

## LAN discovery

| Direction | MID | Body |
|---|---|---|
| terminal → broadcast | `01001` | none (`{"HEADER":{"MID":"01001","VER":"1.0"}}`) |
| monitor → terminal | `11001` | `RSLT`, `PORT` (TLS port), `SYS`, `SYSVER`, `IPADDR` |

`RSLT` 0 = accepted, 1 = retry, 2 = registration already in progress, 3 =
terminal limit reached. A monitor that accepted a first request answers
`RSLT 2` to repeated ones.

## Local pairing (TLS, JSON lines)

The monitor presents a self-signed certificate; the terminal pins its SHA-256
fingerprint after the user approved it, before sending anything. Frames are
UTF-8 JSON objects terminated by `\n` with `HEADER.VER` `"1.0"`.

| Step | Request | Reply | Notes |
|---|---|---|---|
| Code request | `01002` | `11002` `{RSLT, OTPASS}` | the monitor displays a code; `OTPASS` is used for `/registClient` |
| Authentication | `01003` `{OTPASS: <displayed code>}` | `11003` `{RSLT}` | `RSLT` 1-2 wrong code, 3 expired |
| Terminal registration | `01005` `{CLIID, TERMNM, TERMTPC}` | `11005` `{RSLT}` | after `/registClient`; `TERMNM` ≤ 12 characters, `TERMTPC` = receive topic; `RSLT` 1 retry, 2 name too long, 3 name already used, 4 EULA not accepted |

After `11003 RSLT 2` and `11005 RSLT 2/3` the same connection can be reused to
retry the step; other errors require a new connection.

## Cloud API (HTTPS)

| Path | Request `BODY` | Reply `BODY` |
|---|---|---|
| `/registClient` | `OTPASS`, `OSKIND` (`"1"`), `SID`, `SYS`, `SYSVER` | `CLIID`, `URL` (MQTT endpoint), `CERT`, `SECKEY`, `RCVTPC`, `SNDTPC`, `SRVURL` (API origin), optional `SRVROOTCA`, `SYSVER` |
| `/getRoomInfo` | `CLIID`, `SYS`, `SID` | `MGMT` with parallel arrays `ID`, `KIND`, `NAME`, `CAMERA`, `KEY`, `PROP`, `PROCFLG` |
| `/deleteClientInfo` | `CLIID`, `SID`, `SYS` | `CLIID` |

Only HTTP 200 carries a usable reply. A camera entrance station has
`KIND` 5 and `CAMERA` 1.

## Cloud messaging (MQTT)

The terminal subscribes to its receive topic (`<monitor>/…/<client id>`) and to
the monitor-wide topic (the same without `/<client id>`), and publishes on its
send topic. Requests carry `CLIID`, `RCVTPC` (receive topic), `VER` `"1.2"`,
`SYS` `"4"` and `SID`.

### Control

| Request | Reply | Notes |
|---|---|---|
| `26031` get lock status (no `BODY`) | `27031` `{ENTNR, KEYINF[{KEYTP, KEYSTS}]}` | works at any time |
| `26021` unlock, output 1 (`AVID 0`, empty `BODY`) | `27021` `{REASON}` | only during a monitoring session |
| `26121` option output, output 2 | `27121` | only during a monitoring session |
| `26032` lock | `27032` | untested |

### Monitoring and door release

1. `30001` STA_RELAY_REQ with a fresh `CID` (client id + timestamp) and an SDP
   → `31001` (`RSLT 200`).
2. `23021` MNT_REQ (`CKIND 1`, `TMID`/`TMKIND` of the camera station, same
   `CID`) → `24021` (`RSLT 200`; `486` = busy).
3. Door command (`26021` or `26121`), and/or media signaling:
   `30011` ADD_RCV_USER → `31011` with the relay's SDP offer, then `30012`
   UPDATE_RCV_USER with the terminal's SDP answer → `31012`.
4. Teardown: `30031` LEAVE_USER → `31031`, then `23003` DISCNCT_REQ
   (`REASON 16`) → `24003`.

About 60 s after `23021`, the station sends `23003` on the session `CID`; the
terminal answers `30031` then `24003` (`RSLT 200`). Without this answer the
next `23021` gets `486`.

The relay is a Janus gateway: its offer is ICE-lite with PCMU audio and H264
video. All relay requests reuse the session `CID`; `PCLIID` is the `CLIID` of
the last successful reply.

### Incoming call (experimental)

| Event | Message |
|---|---|
| Ring | `23001` on the monitor-wide topic (`CID`, `CLIID` of the station, `DSP1`/`DSP2`, `CKIND`, `TMKIND`, `TMID`) |
| Cancelled by the station | `23004` (answer `24004 RSLT 200 REASON 16`, then `30031`, when the call was taken) |
| Ended | `23003` |
| Take the call | `30011`/`30012` (video), `24001` READY_NFY, `30021` ADD_SND_USER (microphone) → `31021`, `24002` REPLY → `25001`, `23008` CALL_STA_REQ `RSLT 200` → `24008` |
| Decline | `23008` `RSLT 603`, then `30031` |
