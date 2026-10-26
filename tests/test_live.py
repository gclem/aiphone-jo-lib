"""LiveMonitor: negotiation callback, automatic renewal and teardown (no media library)."""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest

from aiphone_jo import (
    AiphoneJoClient,
    CloudCredentials,
    InvalidInputError,
    MediaUnavailableError,
    Output,
    StationBusyError,
    Terminal,
)

from .conftest import JANUS_OFFER, FakeBroker, default_responder, reply_to, settle
from .test_cloud_api import FakeSession

STATION = Terminal(id=102, term_kind=5, name="Entrance", camera=True, ent_key_type=2)


@pytest.fixture
async def client(
    credentials: CloudCredentials, broker: FakeBroker
) -> AsyncIterator[AiphoneJoClient]:
    instance = AiphoneJoClient(credentials, transport_factory=broker, reply_timeout=0.5)
    await instance.connect()
    yield instance
    await instance.disconnect()


class Negotiator:
    def __init__(self) -> None:
        self.offers: list[str] = []
        self.ended = 0

    async def __call__(self, offer: str) -> str:
        self.offers.append(offer)
        return f"v=0\r\nanswer {len(self.offers)}\r\n"

    async def on_end(self) -> None:
        self.ended += 1


async def wait_for(predicate: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_negotiation_and_teardown(client: AiphoneJoClient, broker: FakeBroker) -> None:
    negotiator = Negotiator()
    async with client.live_monitor(negotiator, STATION, on_session_end=negotiator.on_end) as live:
        assert live.session is not None
        assert live.sessions == 1
        assert client.active_session is live.session
    connection = broker.connection
    assert connection.mids == ["30001", "23021", "30011", "30012", "30031", "23003"]
    assert negotiator.offers == [JANUS_OFFER]
    assert connection.headers("30012")[0]["CID"] == connection.headers("30001")[0]["CID"]
    assert connection.published[3][1]["BODY"] == {"SDP": "v=0\r\nanswer 1\r\n"}
    assert negotiator.ended == 1
    assert client.active_session is None
    assert not live.running


async def test_renewal_after_the_station_ends_the_session(
    client: AiphoneJoClient, broker: FakeBroker
) -> None:
    negotiator = Negotiator()
    async with client.live_monitor(negotiator, STATION, on_session_end=negotiator.on_end) as live:
        first = live.session
        assert first is not None
        broker.connection.inject({"HEADER": {"MID": "23003", "CID": first.cid, "REASON": 0}})
        await wait_for(lambda: live.sessions == 2)
        second = live.session
        assert second is not None
        assert second.cid != first.cid
        assert negotiator.ended == 1
        # The door command reuses the live session instead of opening a new one.
        await client.open_door(Output.OPTION)
        await live.renew()
        await wait_for(lambda: live.sessions == 3)
    mids = broker.connection.mids
    assert mids.count("30001") == 3
    assert mids.count("26121") == 1
    assert mids.count("24003") == 1  # answer to the station's 23003
    assert mids[-2:] == ["30031", "23003"]
    assert negotiator.ended == 3
    assert len(negotiator.offers) == 3


async def test_negotiation_failure_closes_the_session(
    client: AiphoneJoClient, broker: FakeBroker
) -> None:
    async def failing(offer: str) -> str:
        raise RuntimeError("no codec")

    with pytest.raises(RuntimeError, match="no codec"):
        async with client.live_monitor(failing, STATION):
            pass
    assert broker.connection.mids[-2:] == ["30031", "23003"]
    assert client.active_session is None


async def test_renewal_gives_up_after_repeated_failures(
    client: AiphoneJoClient, broker: FakeBroker
) -> None:
    negotiator = Negotiator()
    live = client.live_monitor(negotiator, STATION)
    live._retry_delays = (0.01,)
    live._max_failures = 2
    async with live:
        first = live.session
        assert first is not None

        def busy(message: dict[str, Any]) -> list[dict[str, Any]]:
            if message["HEADER"]["MID"] == "23021":
                return [reply_to(message, "24021", rslt=486)]
            return default_responder(message)

        broker.responder = busy
        broker.connection.inject({"HEADER": {"MID": "23003", "CID": first.cid, "REASON": 0}})
        await asyncio.wait_for(live.wait_stopped(), 2)
        assert isinstance(live.error, StationBusyError)
        assert not live.running


async def test_camera_station_selection(credentials: CloudCredentials, broker: FakeBroker) -> None:
    room = {
        "BODY": {
            "MGMT": {"ID": ["101", "102"], "KIND": ["5", "5"], "CAMERA": ["1", "1"]},
        }
    }

    def first_busy(message: dict[str, Any]) -> list[dict[str, Any]]:
        if message["HEADER"]["MID"] == "23021" and message["HEADER"]["TMID"] == 101:
            return [reply_to(message, "24021", rslt=486)]
        return default_responder(message)

    broker.responder = first_busy
    no_http = AiphoneJoClient(credentials, transport_factory=broker)
    await no_http.connect()
    with pytest.raises(InvalidInputError, match="camera station"):
        await no_http.start_camera_session()
    no_http.camera_terminal = 101
    with pytest.raises(StationBusyError):
        await no_http.start_camera_session()
    await no_http.disconnect()

    instance = AiphoneJoClient(
        credentials,
        transport_factory=broker,
        session=FakeSession(payload=room),  # type: ignore[arg-type]
    )
    await instance.connect()
    negotiator = Negotiator()
    async with instance.live_monitor(negotiator) as live:
        assert live.session is not None
        assert live.session.term_id == 102
        live_cid = live.session.cid
        broker.connection.inject({"HEADER": {"MID": "23003", "CID": live_cid, "REASON": 0}})
        await wait_for(lambda: live.sessions == 2)
    tmids = [h["TMID"] for h in broker.connection.headers("23021")]
    # Renewal targets the station that accepted, not the busy one.
    assert tmids == [101, 102, 102]
    await instance.disconnect()


async def test_media_extra_missing(
    client: AiphoneJoClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = importlib.import_module

    def fake_import(name: str, *args: Any) -> Any:
        if name == "aiphone_jo.media":
            raise ImportError("No module named 'aiortc'")
        return real_import(name, *args)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(MediaUnavailableError, match=r"aiphone-jo\[media\]"):
        await client.snapshot(STATION)
    with pytest.raises(MediaUnavailableError):
        client.video_stream(STATION)


async def test_core_import_does_not_load_media_libraries() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys, aiphone_jo, aiphone_jo.cli, aiphone_jo.live; "
        "print(sorted(m for m in ('aiortc', 'av', 'PIL') if m in sys.modules))",
        stdout=asyncio.subprocess.PIPE,
    )
    output, _ = await process.communicate()
    assert output.decode().strip() == "[]"
    await settle()
