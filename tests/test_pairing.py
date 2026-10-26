"""Local TLS pairing against a loopback TLS server."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import socket
import ssl
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from aiphone_jo import (
    DiscoveredMonitor,
    IncompatibleVersionError,
    PairingCloudError,
    PairingError,
    PairingFailureReason,
    PairingFlow,
    PairingLinkClosedError,
    PairingRejectedError,
    PairingStage,
    PairingStep,
    ResponseTimeoutError,
    UntrustedPeerError,
    inspect_peer,
)
from aiphone_jo._util import format_fingerprint
from aiphone_jo.exceptions import PairingTimeoutError
from aiphone_jo.frames import JsonLinesDecoder, Message, frame
from aiphone_jo.pairing import LocalLink

from .conftest import CLIENT_ID, RECV_TOPIC, Pki
from .test_cloud_api import FakeSession, regist_payload

Handler = Callable[[Message], list[bytes]]
REPLIES = {"01002": "11002", "01003": "11003", "01005": "11005"}
CLOSE = b"__close__"


def flow_step(flow: PairingFlow) -> PairingStep:
    return flow.step


def default_handler(message: Message) -> list[bytes]:
    mid = REPLIES.get(message["HEADER"]["MID"])
    if mid is None:
        return []
    body: dict[str, Any] = {"RSLT": 0}
    if mid == "11002":
        body["OTPASS"] = "temporary-otp"
    return [frame(mid, body)]


class TlsMonitor:
    def __init__(self, pki: Pki, handler: Handler) -> None:
        self.pki = pki
        self.handler = handler
        self.seen: list[Message] = []
        self.received_bytes = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    @property
    def fingerprint(self) -> str:
        return format_fingerprint(hashlib.sha256(self.pki.der).digest())

    def context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory, "c.pem"), Path(directory, "k.pem")
            cert.write_text(self.pki.cert_pem)
            key.write_text(self.pki.key_pem)
            context.load_cert_chain(cert, key)
        return context

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        decoder = JsonLinesDecoder()
        with contextlib.suppress(Exception):
            while chunk := await reader.read(4096):
                self.received_bytes += len(chunk)
                for message in decoder.push(chunk):
                    self.seen.append(message)
                    for reply in self.handler(message):
                        if reply == CLOSE:
                            writer.close()
                            return
                        # Fragmented write.
                        writer.write(reply[:11])
                        await writer.drain()
                        await asyncio.sleep(0.005)
                        writer.write(reply[11:])
                        await writer.drain()
        writer.close()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self.context())
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@contextlib.asynccontextmanager
async def tls_monitor(pki: Pki, handler: Handler = default_handler) -> AsyncIterator[TlsMonitor]:
    monitor = TlsMonitor(pki, handler)
    await monitor.start()
    try:
        yield monitor
    finally:
        await monitor.stop()


async def test_inspection_sends_no_application_data(pki: Pki) -> None:
    async with tls_monitor(pki) as monitor:
        peer = await inspect_peer("127.0.0.1", monitor.port)
        await asyncio.sleep(0.05)
    assert peer.fingerprint == monitor.fingerprint
    assert peer.protocol == "TLSv1.2"
    assert peer.subject == {"CN": "localhost"}
    assert monitor.seen == []


async def test_pinned_link_handles_fragmented_frames(pki: Pki) -> None:
    async with tls_monitor(pki) as monitor:
        link = LocalLink()
        try:
            await link.connect("127.0.0.1", monitor.port, fingerprint=monitor.fingerprint.lower())
            assert link.connected
            reply = await link.request("01002", None, "11002")
            assert reply["BODY"]["OTPASS"] == "temporary-otp"
            await link.request("01003", {"OTPASS": "user-code"}, "11003")
            await link.request(
                "01005", {"CLIID": "client", "TERMNM": "Web", "TERMTPC": "own/topic"}, "11005"
            )
        finally:
            await link.close()
    assert monitor.seen[1]["BODY"]["OTPASS"] == "user-code"
    assert monitor.seen[2]["BODY"]["TERMTPC"] == "own/topic"
    assert not link.connected


async def test_wrong_pin_fails_before_sending_anything(pki: Pki) -> None:
    async with tls_monitor(pki) as monitor:
        link = LocalLink()
        with pytest.raises(UntrustedPeerError, match="No message was sent") as err:
            await link.connect("127.0.0.1", monitor.port, fingerprint="00" * 32)
        await asyncio.sleep(0.05)
    assert err.value.peer.fingerprint == monitor.fingerprint
    assert monitor.received_bytes == 0
    assert not link.connected


async def test_rejected_result_and_timeout(pki: Pki) -> None:
    def handler(message: Message) -> list[bytes]:
        if message["HEADER"]["MID"] == "01003":
            return [frame("11003", {"RSLT": 2})]
        return []

    async with tls_monitor(pki, handler) as monitor:
        link = LocalLink()
        await link.connect("127.0.0.1", monitor.port, fingerprint=monitor.fingerprint)
        with pytest.raises(PairingRejectedError, match="RSLT=2") as err:
            await link.request("01003", {"OTPASS": "wrong"}, "11003")
        assert err.value.hint == "authentication failed: wrong code"
        assert err.value.reason is PairingFailureReason.WRONG_CODE
        with pytest.raises(ResponseTimeoutError, match="No 11002"):
            await link.request("01002", None, "11002", timeout=0.05)
        assert not link.connected
        with pytest.raises(PairingLinkClosedError, match="Connect"):
            await link.request("01002", None, "11002")


async def test_connection_errors_are_classified() -> None:
    link = LocalLink()
    with pytest.raises(PairingLinkClosedError) as refused:
        await link.connect("127.0.0.1", _closed_port(), fingerprint="00" * 32, timeout=1)
    assert refused.value.reason is PairingFailureReason.LINK_CLOSED

    async def not_tls(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.0 400 Bad Request\r\n\r\n" * 20)
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()

    server = await asyncio.start_server(not_tls, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(PairingError) as handshake:
            await link.connect("127.0.0.1", port, fingerprint="00" * 32, timeout=2)
    finally:
        server.close()
    assert handshake.value.reason is PairingFailureReason.TLS_FAILED
    assert handshake.value.stage is PairingStage.TLS
    with pytest.raises(PairingError, match="fingerprint or a trust decision"):
        await link.connect("127.0.0.1", 1)


async def test_pairing_flow_end_to_end(pki: Pki) -> None:
    taken = {"count": 1}

    def handler(message: Message) -> list[bytes]:
        if message["HEADER"]["MID"] == "01005" and taken["count"]:
            taken["count"] -= 1
            return [frame("11005", {"RSLT": 3})]
        return default_handler(message)

    session = FakeSession(payload=regist_payload(pki))
    async with tls_monitor(pki, handler) as monitor:
        target = DiscoveredMonitor("127.0.0.1", monitor.port, 4, "00:00")
        async with PairingFlow(session) as flow:  # type: ignore[arg-type]
            peer = await flow.connect(target)
            assert flow.step is PairingStep.AWAITING_TRUST
            with pytest.raises(PairingError, match="Invalid pairing step"):
                await flow.request_code()
            with pytest.raises(UntrustedPeerError):
                flow.approve("00" * 32)
            flow.approve(peer.fingerprint)
            await flow.request_code()
            await flow.authenticate("4321")
            cloud = await flow.register_cloud()
            credentials = await flow.register_terminal("HomeAssist")
    assert cloud.client_id == CLIENT_ID
    assert session.calls[0]["json"]["BODY"]["OTPASS"] == "temporary-otp"
    assert session.calls[0]["json"]["BODY"]["SYSVER"] == "00:00"
    registrations = [m["BODY"] for m in monitor.seen if m["HEADER"]["MID"] == "01005"]
    assert registrations[0] == {"CLIID": CLIENT_ID, "TERMNM": "HomeAssist", "TERMTPC": RECV_TOPIC}
    assert registrations[1]["TERMNM"].startswith("HomeAssis-")
    assert len(registrations[1]["TERMNM"]) <= 12
    assert credentials.term_name == registrations[1]["TERMNM"]
    assert flow_step(flow) is PairingStep.COMPLETED
    assert [m["HEADER"]["MID"] for m in monitor.seen] == ["01002", "01003", "01005", "01005"]


async def test_pairing_flow_with_known_fingerprint(pki: Pki) -> None:
    async with tls_monitor(pki) as monitor:
        target = DiscoveredMonitor("127.0.0.1", monitor.port)
        async with PairingFlow(FakeSession()) as flow:  # type: ignore[arg-type]
            await flow.connect(target, expected_fingerprint=monitor.fingerprint)
            assert flow.step is PairingStep.CONNECTED
        async with PairingFlow(FakeSession()) as flow:  # type: ignore[arg-type]
            with pytest.raises(UntrustedPeerError):
                await flow.connect(target, expected_fingerprint="11" * 32)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def scripted(results: dict[str, list[int]]) -> Callable[[Message], list[bytes]]:
    """Handler answering each MID with the next RSLT of ``results`` (then 0)."""

    def handler(message: Message) -> list[bytes]:
        mid = message["HEADER"]["MID"]
        queue = results.get(mid, [])
        rslt = queue.pop(0) if queue else 0
        if rslt == -1:
            return [CLOSE]
        if mid == "01002" and rslt == 0:
            return [frame("11002", {"RSLT": 0, "OTPASS": "temporary-otp"})]
        return [frame(REPLIES[mid], {"RSLT": rslt})]

    return handler


async def _flow_until(flow: PairingFlow, monitor: TlsMonitor, step: PairingStep) -> None:
    await flow.connect(
        DiscoveredMonitor("127.0.0.1", monitor.port, 4, "00:00"),
        expected_fingerprint=monitor.fingerprint,
    )
    if step is PairingStep.CONNECTED:
        return
    await flow.request_code()
    if step is PairingStep.CODE_REQUESTED:
        return
    await flow.authenticate("1234")
    if step is PairingStep.AUTHENTICATED:
        return
    await flow.register_cloud()


async def test_wrong_code_can_be_retried_on_the_same_connection(pki: Pki) -> None:
    async with tls_monitor(pki, scripted({"01003": [2]})) as monitor:
        async with PairingFlow(FakeSession()) as flow:  # type: ignore[arg-type]
            await _flow_until(flow, monitor, PairingStep.CODE_REQUESTED)
            with pytest.raises(PairingRejectedError) as err:
                await flow.authenticate("0000")
            assert (err.value.stage, err.value.mid, err.value.rslt, err.value.reason) == (
                PairingStage.AUTHENTICATION,
                "11003",
                2,
                PairingFailureReason.WRONG_CODE,
            )
            assert err.value.retry_same_connection
            assert "wrong code" in str(err.value)
            assert flow.connected
            assert flow_step(flow) is PairingStep.CODE_REQUESTED
            await flow.authenticate("1234")
            assert flow_step(flow) is PairingStep.AUTHENTICATED


async def test_expired_code_closes_and_reconnect_resumes(pki: Pki) -> None:
    async with tls_monitor(pki, scripted({"01003": [3]})) as monitor:
        async with PairingFlow(FakeSession()) as flow:  # type: ignore[arg-type]
            with pytest.raises(PairingError, match="Nothing to reconnect"):
                await flow.reconnect()
            with pytest.raises(PairingError) as wrong_step:
                await flow.authenticate("1234")
            assert wrong_step.value.reason is PairingFailureReason.INVALID_STATE
            await _flow_until(flow, monitor, PairingStep.CODE_REQUESTED)
            with pytest.raises(PairingRejectedError) as err:
                await flow.authenticate("1234")
            assert err.value.reason is PairingFailureReason.CODE_EXPIRED
            assert not err.value.retry_same_connection
            assert not flow.connected
            assert flow_step(flow) is PairingStep.IDLE
            with pytest.raises(PairingLinkClosedError, match="reconnect"):
                await flow.authenticate("1234")
            await flow.reconnect()
            assert flow_step(flow) is PairingStep.CONNECTED
            assert flow.approved_fingerprint == monitor.fingerprint
            await flow.request_code()
            await flow.authenticate("1234")


@pytest.mark.parametrize(
    ("rslt", "reason", "retry"),
    [
        (2, PairingFailureReason.NAME_TOO_LONG, True),
        (3, PairingFailureReason.NAME_ALREADY_USED, True),
        (4, PairingFailureReason.EULA_NOT_ACCEPTED, False),
        (1, PairingFailureReason.RETRY, False),
    ],
)
async def test_terminal_registration_failures(
    pki: Pki, rslt: int, reason: PairingFailureReason, retry: bool
) -> None:
    session = FakeSession(payload=regist_payload(pki))
    async with tls_monitor(pki, scripted({"01005": [rslt]})) as monitor:
        async with PairingFlow(session) as flow:  # type: ignore[arg-type]
            await _flow_until(flow, monitor, PairingStep.CLOUD_REGISTERED)
            with pytest.raises(PairingRejectedError) as err:
                await flow.register_terminal("Home", auto_rename=False)
            assert (err.value.stage, err.value.mid, err.value.rslt, err.value.reason) == (
                PairingStage.TERMINAL_REGISTRATION,
                "11005",
                rslt,
                reason,
            )
            assert err.value.retry_same_connection is retry
            assert flow.connected is retry
            if retry:
                credentials = await flow.register_terminal("Hall", auto_rename=False)
                assert credentials.term_name == "Hall"


async def test_monitor_closing_the_link_is_reported(pki: Pki) -> None:
    async with tls_monitor(pki, scripted({"01002": [-1]})) as monitor:
        async with PairingFlow(FakeSession()) as flow:  # type: ignore[arg-type]
            await _flow_until(flow, monitor, PairingStep.CONNECTED)
            with pytest.raises(PairingLinkClosedError) as err:
                await flow.request_code()
            assert (err.value.stage, err.value.reason) == (
                PairingStage.CODE_REQUEST,
                PairingFailureReason.LINK_CLOSED,
            )
            with pytest.raises(PairingLinkClosedError):
                await flow.request_code()
            await flow.reconnect()
            await flow.request_code()


async def test_timeout_is_typed(pki: Pki) -> None:
    async with tls_monitor(pki, lambda message: []) as monitor:
        link = LocalLink()
        await link.connect("127.0.0.1", monitor.port, fingerprint=monitor.fingerprint)
        with pytest.raises(PairingTimeoutError) as err:
            await link.request(
                "01002", None, "11002", timeout=0.05, stage=PairingStage.CODE_REQUEST
            )
        assert (err.value.reason, err.value.mid, err.value.stage) == (
            PairingFailureReason.TIMEOUT,
            "11002",
            PairingStage.CODE_REQUEST,
        )


async def test_incompatible_or_refused_monitor_is_rejected_before_tls() -> None:
    async with PairingFlow(FakeSession()) as flow:  # type: ignore[arg-type]
        with pytest.raises(IncompatibleVersionError) as err:
            await flow.connect(DiscoveredMonitor("127.0.0.1", 1, 2, "00:00"))
        assert (err.value.mid, err.value.rslt, err.value.reason) == (
            "70002",
            1,
            PairingFailureReason.INCOMPATIBLE_VERSION,
        )
        with pytest.raises(PairingRejectedError) as refused:
            await flow.connect(DiscoveredMonitor("127.0.0.1", 0, 4, None, rslt=3))
        assert refused.value.reason is PairingFailureReason.TERMINAL_LIMIT_REACHED


@pytest.mark.parametrize(
    ("status", "error", "reason", "retry"),
    [
        (451, None, PairingFailureReason.EULA_NOT_ACCEPTED, False),
        (400, None, PairingFailureReason.CLOUD_HTTP_ERROR, False),
        (503, None, PairingFailureReason.CLOUD_HTTP_ERROR, True),
        (
            200,
            aiohttp.ClientConnectionError("down"),
            PairingFailureReason.CLOUD_NETWORK_ERROR,
            True,
        ),
        (200, None, PairingFailureReason.CLOUD_INVALID_RESPONSE, False),
    ],
)
async def test_cloud_registration_failures(
    pki: Pki,
    status: int,
    error: Exception | None,
    reason: PairingFailureReason,
    retry: bool,
) -> None:
    session = FakeSession(status, {"BODY": {"CLIID": "x"}})
    session.error = error
    async with tls_monitor(pki) as monitor:
        async with PairingFlow(session) as flow:  # type: ignore[arg-type]
            await _flow_until(flow, monitor, PairingStep.AUTHENTICATED)
            with pytest.raises(PairingCloudError) as err:
                await flow.register_cloud()
            assert (err.value.stage, err.value.reason) == (PairingStage.CLOUD_REGISTRATION, reason)
            assert err.value.retry_same_connection is retry
            if status != 200:
                assert (err.value.mid, err.value.rslt, err.value.status) == (
                    "71001",
                    status,
                    status,
                )
            if retry:
                session.status, session.error = 200, None
                session.payload = regist_payload(pki)
                assert flow_step(flow) is PairingStep.AUTHENTICATED
                assert (await flow.register_cloud()).client_id == CLIENT_ID
            else:
                assert not flow.connected


async def test_cancel_deletes_an_unfinished_registration(pki: Pki) -> None:
    session = FakeSession(payload=regist_payload(pki))
    session.routes["/deleteClientInfo"] = (200, {"BODY": {"CLIID": CLIENT_ID}})
    async with tls_monitor(pki) as monitor:
        flow = PairingFlow(session)  # type: ignore[arg-type]
        await _flow_until(flow, monitor, PairingStep.CLOUD_REGISTERED)
        assert flow.monitor_id is not None
        assert flow.monitor_id.startswith("jo-")
        assert await flow.cancel() is True
        assert session.paths == ["registClient", "deleteClientInfo"]
        assert session.calls[1]["json"]["BODY"]["CLIID"] == CLIENT_ID
        assert not flow.connected
        assert await flow.cancel() is False


async def test_reconnect_drops_an_unfinished_registration(pki: Pki) -> None:
    session = FakeSession(payload=regist_payload(pki))
    session.routes["/deleteClientInfo"] = (500, None)
    async with tls_monitor(pki) as monitor:
        async with PairingFlow(session) as flow:  # type: ignore[arg-type]
            await _flow_until(flow, monitor, PairingStep.CLOUD_REGISTERED)
            await flow.reconnect()
            assert session.paths == ["registClient", "deleteClientInfo"]
            assert flow.monitor_id is None
            assert flow_step(flow) is PairingStep.CONNECTED


async def test_completed_registration_is_never_deleted(pki: Pki) -> None:
    session = FakeSession(payload=regist_payload(pki))
    async with tls_monitor(pki) as monitor:
        flow = PairingFlow(session)  # type: ignore[arg-type]
        await _flow_until(flow, monitor, PairingStep.CLOUD_REGISTERED)
        await flow.register_terminal("Home")
        assert await flow.cancel() is False
    assert session.paths == ["registClient"]
