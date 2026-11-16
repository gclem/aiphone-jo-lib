"""History of this client's monitoring sessions: statuses, restore, retention."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest

from aiphone_jo import (
    AiphoneJoClient,
    CloudCredentials,
    SessionRecord,
    SessionRecordEvent,
    SessionStatus,
    Terminal,
)
from aiphone_jo.client import MAX_NOTABLE_RECORDS, MAX_ROUTINE_RECORDS

from .conftest import FakeBroker, default_responder, settle

STATION = Terminal(id=102, term_kind=5, name="Entrance", camera=True, ent_key_type=2)
NOW = datetime.now().astimezone()


@pytest.fixture
async def client(
    credentials: CloudCredentials, broker: FakeBroker
) -> AsyncIterator[AiphoneJoClient]:
    instance = AiphoneJoClient(credentials, transport_factory=broker, reply_timeout=0.5)
    await instance.connect()
    yield instance
    await instance.disconnect()


def _record(cid: str, status: SessionStatus, age: timedelta = timedelta()) -> SessionRecord:
    return SessionRecord(
        cid=cid,
        term_id=102,
        term_kind=5,
        status=status,
        started_at=NOW - age,
        updated_at=NOW - age,
        cor_cli_id="relay-1",
    )


async def test_statuses_of_a_session(client: AiphoneJoClient) -> None:
    """Starting, active, closed: each change is an event, and kept in the history."""
    events: list[Any] = []
    client.add_listener(events.append)
    session = await client.start_monitor(STATION)
    await session.close()
    statuses = [e.record.status for e in events if isinstance(e, SessionRecordEvent)]
    assert statuses == [SessionStatus.STARTING, SessionStatus.ACTIVE, SessionStatus.CLOSED]
    [record] = client.session_history
    assert record.status is SessionStatus.CLOSED
    assert record.detail == "leave 200, disconnect 200"
    assert record.cid == session.cid
    assert record.tag == session.cid[-20:]
    assert len(record.tag) == 20


async def test_storage_round_trip() -> None:
    """Records survive a restart through their storage form."""
    record = _record("example-client20261116133033123456", SessionStatus.ABANDONED)
    assert SessionRecord.from_storage(record.to_storage()) == record
    assert SessionRecord.from_storage({"cid": "x"}) is None
    assert SessionRecord.from_storage({**record.to_storage(), "status": "?"}) is None


async def test_unfinished_sessions_closed_after_a_restart(
    credentials: CloudCredentials, broker: FakeBroker
) -> None:
    """Restored unfinished sessions are closed again once connected."""
    client = AiphoneJoClient(credentials, transport_factory=broker, reply_timeout=0.5)
    events: list[Any] = []
    client.add_listener(events.append)
    client.restore_sessions(
        [
            _record("given-up-session", SessionStatus.ABANDONED),
            _record("active-at-stop", SessionStatus.ACTIVE),
            _record("closed-session", SessionStatus.CLOSED),
        ]
    )
    history = {r.cid: r for r in client.session_history}
    assert history["active-at-stop"].status is SessionStatus.ABANDONED
    await client.connect()
    try:
        await settle(20)
        closed = {h["CID"] for h in broker.connection.headers("23003")}
        assert closed == {"given-up-session", "active-at-stop"}
        history = {r.cid: r for r in client.session_history}
        assert history["given-up-session"].status is SessionStatus.CLEANED
        assert history["given-up-session"].detail == "connection: leave 200, disconnect 200"
        assert history["closed-session"].status is SessionStatus.CLOSED
        cleaned = [
            e.record.cid
            for e in events
            if isinstance(e, SessionRecordEvent) and e.record.status is SessionStatus.CLEANED
        ]
        assert sorted(cleaned) == ["active-at-stop", "given-up-session"]
        # Once each: not again at the next connection.
        published = len(broker.connection.published)
        await client.disconnect()
        await client.connect()
        await settle(20)
        assert "23003" not in broker.connection.mids[published:]
    finally:
        await client.disconnect()


async def test_cleanup_without_answer_is_tried_again_later(
    credentials: CloudCredentials, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the station answers nothing, the session stays unfinished."""
    monkeypatch.setattr("aiphone_jo.client.END_SESSION_STEP_TIMEOUT", 0.05)

    def silent(message: dict[str, Any]) -> list[dict[str, Any]]:
        if message["HEADER"]["MID"] in ("30031", "23003"):
            return []
        return default_responder(message)

    broker.responder = silent
    client = AiphoneJoClient(credentials, transport_factory=broker, reply_timeout=0.2)
    client.restore_sessions([_record("given-up-session", SessionStatus.ABANDONED)])
    await client.connect()
    try:
        for _ in range(50):
            [record] = client.session_history
            if record.detail:
                break
            await asyncio.sleep(0.05)
        assert record.status is SessionStatus.ABANDONED
        assert record.detail.startswith("connection: leave error")
    finally:
        await client.disconnect()


async def test_history_stays_bounded(credentials: CloudCredentials, broker: FakeBroker) -> None:
    """15 days at most, the last ordinary sessions, a bounded number of others."""
    client = AiphoneJoClient(credentials, transport_factory=broker, reply_timeout=0.5)
    client.restore_sessions(
        [_record(f"old-{i}", SessionStatus.ABANDONED, timedelta(days=16)) for i in range(5)]
        + [_record(f"closed-{i}", SessionStatus.CLOSED) for i in range(40)]
        + [_record(f"refused-{i}", SessionStatus.REFUSED) for i in range(40)]
        + [_record(f"abandoned-{i}", SessionStatus.ABANDONED) for i in range(30)]
    )
    history = client.session_history
    cids = {r.cid for r in history}
    assert not any(cid.startswith("old-") for cid in cids)
    routine = [r for r in history if r.status is SessionStatus.CLOSED]
    assert len(routine) == MAX_ROUTINE_RECORDS
    assert routine[-1].cid == "closed-39"
    notable = [r for r in history if r.status is not SessionStatus.CLOSED]
    assert len(notable) == MAX_NOTABLE_RECORDS
    # The unfinished ones are kept; resolved ones went first.
    assert sum(r.status is SessionStatus.ABANDONED for r in notable) == 30
