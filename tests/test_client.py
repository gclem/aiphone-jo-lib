"""AiphoneJoClient orchestration: door release, sessions, ring and call handling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest

from aiphone_jo import (
    AiphoneJoClient,
    CallStatus,
    CallUpdatedEvent,
    CloudAuthError,
    CloudCredentials,
    CommandRejectedError,
    ConnectionStateEvent,
    Gate,
    InvalidCredentialsError,
    InvalidInputError,
    MessageEvent,
    NotConnectedError,
    Output,
    ResponseTimeoutError,
    RingEvent,
    SessionEndedBy,
    SessionEndedEvent,
    SessionError,
    StationBusyError,
    Terminal,
)

from .conftest import (
    BROADCAST_TOPIC,
    JANUS_OFFER,
    SEND_TOPIC,
    FakeBroker,
    default_responder,
    reply_to,
    settle,
)
from .test_cloud_api import FakeSession


def current_status(client: AiphoneJoClient) -> CallStatus | None:
    return client.current_call.status if client.current_call else None


STATION = Terminal(id=102, term_kind=5, name="Entrance", camera=True, ent_key_type=2)


@pytest.fixture
async def client(
    credentials: CloudCredentials, broker: FakeBroker
) -> AsyncIterator[AiphoneJoClient]:
    instance = AiphoneJoClient(
        credentials,
        transport_factory=broker,
        reply_timeout=0.5,
        reconnect_delays=(0.01,),
    )
    await instance.connect()
    yield instance
    await instance.disconnect()


def record(client: AiphoneJoClient) -> list[Any]:
    events: list[Any] = []
    client.add_listener(events.append)
    return events


def ring(cid: str, ckind: int = 1) -> dict[str, Any]:
    return {
        "HEADER": {
            "MID": "23001",
            "CID": cid,
            "CLIID": "station",
            "DSP1": "Entrance",
            "DSP2": "@",
            "CKIND": ckind,
            "TMKIND": 5,
            "TMID": 102,
            "RSLT": 0,
        }
    }


async def test_connect_subscribes_to_both_topics(
    client: AiphoneJoClient, broker: FakeBroker, credentials: CloudCredentials
) -> None:
    options = broker.options[0]
    assert options.topics == (credentials.recv_topic, BROADCAST_TOPIC)
    assert options.port == 8883
    assert options.ssl_context is not None
    assert client.connected
    assert client.broadcast_subscribed


async def test_refused_broadcast_topic_is_reported(
    credentials: CloudCredentials, broker: FakeBroker
) -> None:
    broker.granted_broadcast = False
    instance = AiphoneJoClient(credentials, transport_factory=broker)
    events = record(instance)
    await instance.connect()
    assert not instance.broadcast_subscribed
    assert events == [ConnectionStateEvent(True, False)]
    await instance.disconnect()
    assert events[-1] == ConnectionStateEvent(False, False, None)


def test_send_topic_is_required(credentials: CloudCredentials) -> None:
    with pytest.raises(InvalidCredentialsError):
        AiphoneJoClient(replace(credentials, send_topic=None))


async def test_get_lock_status(client: AiphoneJoClient, broker: FakeBroker) -> None:
    status = await client.get_lock_status()
    assert status.entrance_count == 2
    assert [(k.key_type, k.key_status) for k in status.keys] == [(2, 0), (2, 0)]
    topic, message = broker.connection.published[0]
    assert (topic, message["HEADER"]["MID"]) == (SEND_TOPIC, "26031")


async def test_get_lock_status_rejected_and_timeout(
    client: AiphoneJoClient, broker: FakeBroker
) -> None:
    broker.responder = lambda m: [reply_to(m, "27031", rslt=1)]
    with pytest.raises(CommandRejectedError):
        await client.get_lock_status()
    broker.responder = lambda m: []
    with pytest.raises(ResponseTimeoutError, match="27031"):
        await client.get_lock_status()


@pytest.mark.parametrize(
    ("output", "mid"),
    [(Output.UNLOCK, "26021"), (Output.OPTION, "26121"), (Gate.MAIN, "26021"), (2, "26121")],
)
async def test_open_door_runs_the_door_release_sequence(
    client: AiphoneJoClient, broker: FakeBroker, output: Output | int, mid: str
) -> None:
    events = record(client)
    await client.open_door(output, STATION)
    connection = broker.connection
    assert connection.mids == ["30001", "23021", mid, "30031", "23003"]
    start, monitor, leave, disconnect = (
        connection.headers(m)[0] for m in ("30001", "23021", "30031", "23003")
    )
    assert start["CID"] == monitor["CID"] == leave["CID"] == disconnect["CID"]
    assert (monitor["TMID"], monitor["TMKIND"], monitor["CKIND"]) == (102, 5, 1)
    assert leave["PCLIID"] == "station-1"
    assert disconnect["REASON"] == 16
    assert "a=sendonly" in connection.published[0][1]["BODY"]["SDP"]
    assert [e for e in events if isinstance(e, SessionEndedEvent)] == [
        SessionEndedEvent(start["CID"], SessionEndedBy.CLIENT)
    ]
    assert client.active_session is None
    await client.open_door(output, 102)
    assert connection.mids.count("30001") == 2
    assert connection.headers("30001")[1]["CID"] != start["CID"]


async def test_open_door_requires_a_station(client: AiphoneJoClient) -> None:
    with pytest.raises(InvalidInputError):
        await client.open_door(Output.UNLOCK)


async def test_rejected_unlock_still_closes_the_session(
    client: AiphoneJoClient, broker: FakeBroker
) -> None:
    def responder(message: dict[str, Any]) -> list[dict[str, Any]]:
        if message["HEADER"]["MID"] == "26021":
            return [reply_to(message, "27021", rslt=1)]
        return default_responder(message)

    broker.responder = responder
    with pytest.raises(CommandRejectedError, match="UNLOCK_REQ"):
        await client.open_door(Output.UNLOCK, STATION)
    assert broker.connection.mids == ["30001", "23021", "26021", "30031", "23003"]
    assert client.active_session is None


async def test_busy_station_raises_and_releases_the_relay(
    client: AiphoneJoClient, broker: FakeBroker, replies: dict[str, Any]
) -> None:
    def responder(message: dict[str, Any]) -> list[dict[str, Any]]:
        if message["HEADER"]["MID"] == "23021":
            busy = dict(replies["24021_busy"])
            busy["HEADER"] = {**busy["HEADER"], "CID": message["HEADER"]["CID"]}
            return [busy]
        return default_responder(message)

    broker.responder = responder
    with pytest.raises(StationBusyError) as err:
        await client.open_door(Output.UNLOCK, STATION)
    assert err.value.rslt == 486
    assert broker.connection.mids == ["30001", "23021", "30031"]
    broker.responder = default_responder
    await client.open_door(Output.UNLOCK, STATION)


async def test_rejected_relay_start(
    client: AiphoneJoClient, broker: FakeBroker, replies: dict[str, Any]
) -> None:
    def responder(message: dict[str, Any]) -> list[dict[str, Any]]:
        rejected = dict(replies["31001_rejected"])
        rejected["HEADER"] = {**rejected["HEADER"], "CID": message["HEADER"]["CID"]}
        return [rejected]

    broker.responder = responder
    with pytest.raises(CommandRejectedError, match="RSLT=400"):
        await client.open_door(Output.UNLOCK, STATION)
    assert broker.connection.mids == ["30001"]
    assert client.active_session is None


async def test_replies_with_another_cid_are_ignored(
    client: AiphoneJoClient, broker: FakeBroker
) -> None:
    def responder(message: dict[str, Any]) -> list[dict[str, Any]]:
        if message["HEADER"]["MID"] == "30001":
            stale = reply_to(message, "31001", rslt=468, header={"CID": "old-session"})
            return [stale, *default_responder(message)]
        return default_responder(message)

    broker.responder = responder
    await client.open_door(Output.UNLOCK, STATION)


async def test_monitor_session_media_signaling(client: AiphoneJoClient, broker: FakeBroker) -> None:
    async with client.monitor(STATION) as session:
        assert client.active_session is session
        offer = await session.request_offer()
        assert offer == JANUS_OFFER
        await session.send_answer("v=0\r\nanswer\r\n")
        await client.open_door(Output.OPTION)
    connection = broker.connection
    assert connection.mids == ["30001", "23021", "30011", "30012", "26121", "30031", "23003"]
    assert connection.published[3][1]["BODY"] == {"SDP": "v=0\r\nanswer\r\n"}
    assert connection.headers("30011")[0]["PCLIID"] == "station-1"
    with pytest.raises(SessionError):
        await session.unlock()


async def test_station_closes_the_session(client: AiphoneJoClient, broker: FakeBroker) -> None:
    events = record(client)
    session = await client.start_monitor(STATION)
    connection = broker.connection
    published = len(connection.published)
    connection.inject({"HEADER": {"MID": "23003", "CID": "other-session", "REASON": 0}})
    await settle()
    assert len(connection.published) == published
    connection.inject({"HEADER": {"MID": "23003", "CID": session.cid, "REASON": 0}})
    await settle(10)
    assert connection.mids[published:] == ["30031", "24003"]
    reply = connection.headers("24003")[0]
    assert (reply["RSLT"], reply["REASON"], reply["CID"]) == (200, 0, session.cid)
    assert not session.active
    assert session.ended_by is SessionEndedBy.STATION
    assert SessionEndedEvent(session.cid, SessionEndedBy.STATION) in events
    connection.inject({"HEADER": {"MID": "23003", "CID": session.cid, "REASON": 0}})
    await settle()
    assert await session.close() == {}
    assert connection.mids[published:] == ["30031", "24003"]
    await client.open_door(Output.UNLOCK, STATION)


async def test_ring_tracking(client: AiphoneJoClient, broker: FakeBroker) -> None:
    events = record(client)
    connection = broker.connection
    connection.inject(ring("station-cid-1"), BROADCAST_TOPIC)
    connection.inject(ring("station-cid-1"))
    await settle()
    rings = [e for e in events if isinstance(e, RingEvent)]
    assert len(rings) == 1
    call = rings[0].call
    assert (call.cid, call.status, call.display_name, call.term_id, call.topic) == (
        "station-cid-1",
        CallStatus.RINGING,
        "Entrance",
        102,
        BROADCAST_TOPIC,
    )
    assert "station-cid-1" not in repr(call)
    connection.inject({"HEADER": {"MID": "23004", "CID": "other", "RSLT": 0}})
    connection.inject(
        {"HEADER": {"MID": "24002", "CID": "station-cid-1", "CLIID": "phone", "RSLT": 200}}
    )
    connection.inject({"HEADER": {"MID": "23004", "CID": "station-cid-1", "RSLT": 0}})
    await settle()
    assert call.status is CallStatus.CANCELLED
    assert [s.mid for s in call.signals] == ["24002", "23004"]
    assert call.ended_at is not None
    assert CallUpdatedEvent(call) in events
    assert connection.published == []


async def test_ring_expires(credentials: CloudCredentials, broker: FakeBroker) -> None:
    instance = AiphoneJoClient(credentials, transport_factory=broker, ring_timeout=0.02)
    await instance.connect()
    broker.connection.inject(ring("call-x"))
    await asyncio.sleep(0.1)
    assert instance.current_call is not None
    assert instance.current_call.status is CallStatus.EXPIRED
    await instance.disconnect()


async def test_answer_call_full_sequence(client: AiphoneJoClient, broker: FakeBroker) -> None:
    connection = broker.connection
    with pytest.raises(SessionError, match="No ringing call"):
        await client.answer_call()
    connection.inject(ring("call-1", ckind=3), BROADCAST_TOPIC)
    await settle()
    session = await client.answer_call()
    assert client.current_call is not None
    assert client.current_call.engaged
    assert await session.request_offer() == JANUS_OFFER
    await session.ready()
    assert await session.add_send_audio("v=0\r\noffer\r\n") == "v=0\r\nanswer\r\n"
    await session.reply()
    assert await session.start()
    assert client.current_call.status is CallStatus.TALKING
    sent = [m["HEADER"] for _, m in connection.published]
    assert [(h["MID"], h["CID"], h["RSLT"], h.get("CKIND")) for h in sent] == [
        ("30011", "call-1", 0, None),
        ("24001", "call-1", 0, 0),
        ("30021", "call-1", 0, None),
        ("24002", "call-1", 200, 3),
        ("23008", "call-1", 200, 3),
    ]
    assert sent[0]["PCLIID"] == "station"
    assert sent[1]["DSP1"] == ""
    await session.close()
    assert connection.mids[-2:] == ["30031", "23003"]
    assert current_status(client) is CallStatus.ENDED


async def test_reject_call(client: AiphoneJoClient, broker: FakeBroker) -> None:
    connection = broker.connection
    connection.inject(ring("call-2"))
    await settle()
    session = await client.answer_call()
    await session.reject()
    assert [(h["MID"], h["RSLT"]) for _, m in connection.published for h in [m["HEADER"]]] == [
        ("23008", 603),
        ("30031", 0),
    ]
    assert client.current_call is not None
    assert client.current_call.status is CallStatus.REJECTED
    assert not client.current_call.engaged
    assert client.active_session is None
    await client.open_door(Output.UNLOCK, STATION)


async def test_station_cancels_an_engaged_call(client: AiphoneJoClient, broker: FakeBroker) -> None:
    connection = broker.connection
    connection.inject(ring("call-3"))
    await settle()
    session = await client.answer_call()
    connection.inject({"HEADER": {"MID": "23004", "CID": "call-3", "RSLT": 0}})
    await settle(10)
    assert [
        (h["MID"], h["RSLT"], h.get("REASON"))
        for _, m in connection.published
        for h in [m["HEADER"]]
    ] == [("24004", 200, 16), ("30031", 0, None)]
    assert client.current_call is not None
    assert client.current_call.status is CallStatus.CANCELLED
    assert not session.active
    await session.close()
    assert client.active_session is None


async def test_answer_call_refused_while_monitoring(
    client: AiphoneJoClient, broker: FakeBroker
) -> None:
    async with client.monitor(STATION):
        broker.connection.inject(ring("call-4"))
        await settle()
        with pytest.raises(SessionError, match="already open"):
            await client.answer_call()


async def test_connection_loss_and_reconnection(
    client: AiphoneJoClient, broker: FakeBroker
) -> None:
    events = record(client)
    session = await client.start_monitor(STATION)
    broker.fail_next = 1
    broker.connection.drop()
    await settle()
    assert not client.connected
    assert not session.active
    assert session.ended_by is SessionEndedBy.CONNECTION_LOST
    with pytest.raises(NotConnectedError):
        await client.get_lock_status()
    for _ in range(100):
        if client.connected:
            break
        await asyncio.sleep(0.01)
    assert client.connected
    assert len(broker.options) == 3
    states = [e.connected for e in events if isinstance(e, ConnectionStateEvent)]
    assert states == [False, True]
    await session.close()
    await client.open_door(Output.UNLOCK, STATION)


async def test_pending_request_fails_when_the_connection_drops(
    client: AiphoneJoClient, broker: FakeBroker
) -> None:
    broker.responder = lambda m: []
    task = asyncio.create_task(client.get_lock_status())
    await settle()
    broker.connection.drop()
    with pytest.raises(NotConnectedError):
        await task


async def test_disconnect_closes_the_open_session(
    credentials: CloudCredentials, broker: FakeBroker
) -> None:
    instance = AiphoneJoClient(credentials, transport_factory=broker)
    await instance.connect()
    await instance.start_monitor(STATION)
    await instance.disconnect()
    assert broker.connection.mids[-2:] == ["30031", "23003"]
    assert broker.connection.closed
    assert not instance.connected


async def test_invalid_payloads_and_listener_errors_are_contained(
    client: AiphoneJoClient, broker: FakeBroker, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(event: object) -> None:
        raise RuntimeError("listener bug")

    client.add_listener(broken)
    remove = client.add_listener(lambda event: None)
    remove()
    remove()
    events = record(client)
    connection = broker.connection
    connection.inject_raw(b"not json")
    connection.inject_raw(b"x" * 20000)
    connection.inject({"HEADER": {"MID": "23001", "CID": 5}})
    connection.inject({"HEADER": {"MID": "99999"}})
    await settle()
    assert [type(e) for e in events] == [MessageEvent, MessageEvent]
    assert "listener bug" not in caplog.text or "Error in an aiphone_jo listener" in caplog.text
    assert client.current_call is None
    assert await client.get_lock_status()


async def test_get_room_info_requires_a_session(credentials: CloudCredentials) -> None:
    with pytest.raises(InvalidInputError):
        await AiphoneJoClient(credentials).get_room_info()
    session = FakeSession(
        payload={"BODY": {"MGMT": {"ID": ["102"], "KIND": ["5"], "CAMERA": ["1"]}}}
    )
    info = await AiphoneJoClient(credentials, session=session).get_room_info()  # type: ignore[arg-type]
    assert info.camera_terminals[0].id == 102


async def test_debug_logs_never_contain_identity_or_secrets(
    client: AiphoneJoClient,
    broker: FakeBroker,
    credentials: CloudCredentials,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="aiphone_jo")
    await client.open_door(Output.UNLOCK, STATION)
    broker.connection.inject(ring("call-secret"))
    await settle()
    text = caplog.text
    assert "26021" in text
    for value in (
        credentials.client_id,
        credentials.recv_topic,
        str(credentials.send_topic),
        credentials.host,
        "call-secret",
        "BEGIN",
        "ice-pwd",
    ):
        assert value not in text
    assert credentials.client_id not in repr(credentials)
    assert "BEGIN" not in repr(credentials)


async def test_auth_failure_stops_reconnection(client: AiphoneJoClient, broker: FakeBroker) -> None:
    events = record(client)
    broker.auth_fail = True
    broker.connection.drop()
    for _ in range(100):
        if client.auth_failed:
            break
        await asyncio.sleep(0.01)
    assert client.auth_failed
    attempts = len(broker.options)
    await asyncio.sleep(0.05)
    assert len(broker.options) == attempts
    states = [e for e in events if isinstance(e, ConnectionStateEvent)]
    assert states[-1] == ConnectionStateEvent(False, False, "not authorized", auth_failed=True)
    assert not client.connected


async def test_connect_reports_auth_failure(
    credentials: CloudCredentials, broker: FakeBroker
) -> None:
    broker.auth_fail = True
    instance = AiphoneJoClient(credentials, transport_factory=broker)
    with pytest.raises(CloudAuthError):
        await instance.connect()
    assert instance.auth_failed
    broker.auth_fail = False
    await instance.connect()
    assert not instance.auth_failed
    await instance.disconnect()


def test_output_enum_is_backward_compatible() -> None:
    assert Gate is Output
    assert getattr(Gate, "MAIN") is Output.UNLOCK  # noqa: B009 - alias lookup
    assert Gate.OPTION is Output.OPTION
    assert list(Output) == [Output.UNLOCK, Output.OPTION]
    assert (Output.UNLOCK.value, Output.OPTION.value) == ("main", "option")
    assert (Output.UNLOCK.number, Output.OPTION.number) == (1, 2)
    assert Output.coerce("main") is Output.UNLOCK
    assert Output.coerce(2) is Output.OPTION
    for bad in (0, 3, True, "large"):
        with pytest.raises(ValueError, match=r"output|valid"):
            Output.coerce(bad)


async def test_open_door_output_arguments(client: AiphoneJoClient, broker: FakeBroker) -> None:
    with pytest.warns(DeprecationWarning, match="'gate' keyword"):
        await client.open_door(terminal=STATION, gate=Gate.OPTION)
    assert broker.connection.mids[2] == "26121"
    with pytest.raises(InvalidInputError, match="expected 1 or 2"):
        await client.open_door(3, STATION)
    async with client.monitor(STATION) as session:
        await session.open(1)
        with pytest.warns(DeprecationWarning, match="gate"):
            await session.open(gate=Output.OPTION)
    assert broker.connection.mids[-4:-2] == ["26021", "26121"]
