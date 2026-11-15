# Calls

[← README](../README.md)

## Rings

When a station rings (23001), the client emits a `RingEvent` with the
`IncomingCall`. Then every change of the call emits a `CallUpdatedEvent`:

- a cancel by the station (23004);
- an answer by another phone;
- an end of the call.

When a call ends, `IncomingCall.end_reason` and `CallUpdatedEvent.reason` are
set to a `CallEndReason`:

| Reason | Cause |
|---|---|
| `answered_elsewhere` | Another phone answered first (status `answered_elsewhere`) |
| `ended_by_station` | The station hung up (`23003`) |
| `cancelled_by_station` | The visitor or the station abandoned the call (`23004`) |
| `expired` | Nobody answered within `ring_timeout` (60 s by default) |
| `rejected` | This client declined (`CallSession.reject()`) |
| `ended_by_client` | This client hung up |

The maximum ring and conversation durations enforced by the station are
unknown.

## Picture of a ringing call

`CallSession.withdraw()` leaves the relay (`30031` only) without answering or
declining: no `24001`, `24002`, `23008` nor `23003` is sent. The call keeps
ringing on the other phones and can be answered later. For example, to grab a
picture of the visitor while the call rings:

```python
session = await client.answer_call(call)
try:
    offer = await session.request_offer()
    await session.send_answer(await negotiate(offer))
    ...  # receive one video frame
finally:
    await session.withdraw()
```

The Home Assistant integration uses this for its snapshot on ring, on real
installations.

## Answering a call (experimental)

`answer_call()` runs the whole pick-up of the ringing call and hangs up when
the block exits:

```python
async def negotiate(offer_sdp: str) -> str:
    ...  # same contract as live_monitor(): complete answer to the relay's offer

async def talk(send_offer) -> None:            # optional: our microphone
    offer = ...                                # SDP offer of our audio sender
    answer = await send_offer(offer)           # ADD_SND_USER_REQ 30021 -> 31021
    ...                                        # apply the relay's answer

async with client.answer_call(call, negotiate, talk=talk) as session:
    ...  # talking (CallStatus.TALKING)
```

The sequence:

1. `30011` (relay offer) → `negotiate` → `30012` → `24001`.
2. If `talk` is given, `talk(send_offer)` (`30021` → `31021`). Without `talk`,
   this step is skipped: the call is answered, but no audio is sent to the
   station.
3. `24002` → `25001`, and `23008` (`RSLT 200`) → `24008`.

What happens on failure, and the other ways to use it:

- **If another phone answered first** (`25001` naming another client),
  `CallAnsweredElsewhereError` is raised. Only the relay is left (`30031`):
  the call is not hung up for the other phone.
- **On any other failure or cancellation**, a call that was ours is hung up
  (`30031` then `23003`). Otherwise only the relay is left, and the call keeps
  ringing on the other phones. `CallEndedError.reason` tells why a call ended
  during the sequence.
- **`await client.answer_call(call, negotiate)`** returns the `CallSession`
  without hanging up at the end: call `close()` yourself.
- **`await client.answer_call()`** returns the session before any message is
  sent, for a manual sequence.
