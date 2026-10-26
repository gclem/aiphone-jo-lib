"""UDP discovery on the loopback interface."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from aiphone_jo import (
    DiscoveryError,
    NoMonitorFoundError,
    PairingFailureReason,
    PairingRejectedError,
    PairingStage,
    discover,
)
from aiphone_jo.discovery import DiscoveredMonitor, broadcast_targets
from aiphone_jo.frames import frame, parse_frame


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class FakeMonitor(asyncio.DatagramProtocol):
    def __init__(self, reply_port: int, replies: list[bytes]) -> None:
        self.reply_port = reply_port
        self.replies = replies
        self.requests: list[dict[str, Any]] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.requests.append(parse_frame(data))
        assert self.transport is not None
        for reply in self.replies:
            self.transport.sendto(reply, ("127.0.0.1", self.reply_port))


async def start_monitor(replies: list[bytes]) -> tuple[FakeMonitor, int, int]:
    receive_port = free_udp_port()
    transport, monitor = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: FakeMonitor(receive_port, replies), local_addr=("127.0.0.1", 0)
    )
    send_port = transport.get_extra_info("sockname")[1]
    return monitor, send_port, receive_port


async def test_discovery_deduplicates_and_uses_sender_ip(replies: dict[str, Any]) -> None:
    reply = frame("11001", replies["11001"]["BODY"])
    monitor, send_port, receive_port = await start_monitor([reply, reply])
    try:
        result = await discover(
            "127.0.0.1",
            bind_address="127.0.0.1",
            send_port=send_port,
            receive_port=receive_port,
            timeout=1,
        )
    finally:
        assert monitor.transport is not None
        monitor.transport.close()
    assert len(monitor.requests) == 1
    assert monitor.requests[0]["HEADER"] == {"MID": "01001", "VER": "1.0"}
    assert result == [DiscoveredMonitor("127.0.0.1", 50000, 4, "00:00")]
    assert result[0].sys_type == 4


async def test_invalid_replies_are_ignored() -> None:
    replies = [
        frame("11001", {"RSLT": 0, "PORT": 70000}),
        frame("11001", {"RSLT": "0", "PORT": 1}),
        frame("11002", {"RSLT": 0, "PORT": 1}),
        b"not json",
    ]
    monitor, send_port, receive_port = await start_monitor(replies)
    try:
        with pytest.raises(NoMonitorFoundError, match="No monitor"):
            await discover(
                ["127.0.0.1", "127.0.0.2"],
                bind_address="127.0.0.1",
                send_port=send_port,
                receive_port=receive_port,
                timeout=0.3,
            )
    finally:
        assert monitor.transport is not None
        monitor.transport.close()


async def test_udp_failures_are_explicit() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        with pytest.raises(DiscoveryError, match="UDP"):
            await discover("127.0.0.1", bind_address="127.0.0.1", receive_port=port, timeout=0.1)
    with pytest.raises(DiscoveryError):
        await discover([], bind_address="127.0.0.1")


def test_broadcast_targets_always_include_limited_broadcast() -> None:
    targets = broadcast_targets()
    assert targets[-1] == "255.255.255.255"
    assert len(targets) == len(set(targets))


async def _run(replies: list[bytes], **kwargs: Any) -> list[DiscoveredMonitor]:
    monitor, send_port, receive_port = await start_monitor(replies)
    try:
        return await discover(
            ["127.0.0.1", "127.0.0.2"],
            bind_address="127.0.0.1",
            send_port=send_port,
            receive_port=receive_port,
            timeout=0.3,
            **kwargs,
        )
    finally:
        assert monitor.transport is not None
        monitor.transport.close()


@pytest.mark.parametrize(
    ("rslt", "reason"),
    [
        (1, PairingFailureReason.RETRY),
        (2, PairingFailureReason.REGISTRATION_IN_PROGRESS),
        (3, PairingFailureReason.TERMINAL_LIMIT_REACHED),
    ],
)
async def test_refusals_are_reported(rslt: int, reason: PairingFailureReason) -> None:
    with pytest.raises(PairingRejectedError) as err:
        await _run([frame("11001", {"RSLT": rslt, "SYS": 4})])
    assert (err.value.mid, err.value.rslt, err.value.reason) == ("11001", rslt, reason)
    assert err.value.stage is PairingStage.DISCOVERY
    refused = await _run([frame("11001", {"RSLT": rslt, "SYS": 4})], include_refused=True)
    assert refused == [DiscoveredMonitor("127.0.0.1", 0, 4, None, rslt)]
    assert not refused[0].accepted


async def test_acceptance_wins_over_a_later_refusal_from_the_same_monitor() -> None:
    # The monitor answers RSLT 2 to the repeated 01001 after accepting the first one.
    replies = [frame("11001", {"RSLT": 0, "PORT": 50000, "SYS": 4}), frame("11001", {"RSLT": 2})]
    result = await _run(replies, include_refused=True)
    assert result == [DiscoveredMonitor("127.0.0.1", 50000, 4, None, 0)]
    assert result[0].accepted
