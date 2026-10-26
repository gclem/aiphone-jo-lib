"""aiomqtt adapter against a minimal mutual-TLS MQTT 3.1.1 broker."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from aiphone_jo import CloudAuthError, CloudConnectionError, CloudCredentials
from aiphone_jo.transport import MqttConnectOptions, aiomqtt_connect, is_auth_failure

from .conftest import BROADCAST_TOPIC, CLIENT_ID, RECV_TOPIC, Pki, make_pki


def packet(kind: int, data: bytes) -> bytes:
    length, remaining = bytearray(), len(data)
    while True:
        digit, remaining = remaining % 128, remaining // 128
        length.append(digit | (128 if remaining else 0))
        if not remaining:
            break
    return bytes([kind]) + bytes(length) + data


def utf8(text: str) -> bytes:
    raw = text.encode()
    return len(raw).to_bytes(2, "big") + raw


class Broker:
    def __init__(
        self,
        pki: Pki,
        *,
        connack: int = 0,
        refuse: tuple[str, ...] = (),
        trust: Pki | None = None,
    ) -> None:
        self.pki = pki
        self.trust = trust or pki
        self.connack = connack
        self.refuse = refuse
        self.subscriptions: list[str] = []
        self.published: list[tuple[str, bytes]] = []
        self.client_cert: dict[str, object] | None = None
        self.port = 0
        self._server: asyncio.Server | None = None

    def context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory, "c.pem"), Path(directory, "k.pem")
            cert.write_text(self.pki.cert_pem)
            key.write_text(self.pki.key_pem)
            context.load_cert_chain(cert, key)
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=self.trust.cert_pem)
        # TLS 1.2: a rejected client certificate is reported during the handshake.
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        return context

    async def _read_packet(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        first = (await reader.readexactly(1))[0]
        length, multiplier = 0, 1
        while True:
            digit = (await reader.readexactly(1))[0]
            length += (digit & 127) * multiplier
            multiplier *= 128
            if not digit & 128:
                break
        return first, await reader.readexactly(length)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.client_cert = writer.get_extra_info("peercert")
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            while True:
                first, data = await self._read_packet(reader)
                kind = first >> 4
                if kind == 1:
                    writer.write(packet(0x20, bytes([0, self.connack])))
                elif kind == 8:
                    topics, index = [], 2
                    while index < len(data):
                        size = int.from_bytes(data[index : index + 2], "big")
                        topics.append(data[index + 2 : index + 2 + size].decode())
                        index += 2 + size + 1
                    self.subscriptions += topics
                    codes = bytes(0x80 if topic in self.refuse else 0 for topic in topics)
                    writer.write(packet(0x90, data[:2] + codes))
                    writer.write(
                        packet(0x30, utf8(BROADCAST_TOPIC) + b'{"HEADER":{"MID":"23001"}}')
                    )
                elif kind == 3:
                    size = int.from_bytes(data[:2], "big")
                    self.published.append((data[2 : 2 + size].decode(), data[2 + size :]))
                elif kind == 12:
                    writer.write(packet(0xD0, b""))
                elif kind == 14:
                    break
                await writer.drain()
        writer.close()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self.context())
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 1)


@contextlib.asynccontextmanager
async def broker(pki: Pki, **kwargs: object) -> AsyncIterator[Broker]:
    server = Broker(pki, **kwargs)  # type: ignore[arg-type]
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def options(pki: Pki, port: int, *, trusted: bool = True) -> MqttConnectOptions:
    # Direct construction: CloudCredentials.parse() refuses 127.0.0.1 on purpose.
    credentials = CloudCredentials(
        host="127.0.0.1",
        client_id=CLIENT_ID,
        recv_topic=RECV_TOPIC,
        cert_pem=pki.cert_pem,
        key_pem=pki.key_pem,
        root_ca_pem=pki.cert_pem if trusted else None,
    )
    return MqttConnectOptions(
        host="127.0.0.1",
        port=port,
        client_id=CLIENT_ID,
        topics=(RECV_TOPIC, BROADCAST_TOPIC),
        ssl_context=credentials.create_ssl_context(),
        timeout=3,
    )


async def test_mutual_tls_subscribe_receive_publish(pki: Pki) -> None:
    async with broker(pki) as server:
        connection = await aiomqtt_connect(options(pki, server.port))
        try:
            assert server.client_cert
            assert server.subscriptions == [RECV_TOPIC, BROADCAST_TOPIC]
            assert dict(connection.granted) == {RECV_TOPIC: True, BROADCAST_TOPIC: True}
            messages = connection.messages()
            topic, payload = await asyncio.wait_for(anext(messages), 2)
            assert (topic, payload) == (BROADCAST_TOPIC, b'{"HEADER":{"MID":"23001"}}')
            await connection.publish("send/topic", b"{}")
            for _ in range(50):
                if server.published:
                    break
                await asyncio.sleep(0.02)
            assert server.published == [("send/topic", b"{}")]
        finally:
            await connection.close()
            await connection.close()


@pytest.mark.parametrize("code", [4, 5])
async def test_not_authorized_connack_is_an_auth_error(pki: Pki, code: int) -> None:
    async with broker(pki, connack=code) as server:
        with pytest.raises(CloudAuthError, match="refused this identity"):
            await aiomqtt_connect(options(pki, server.port))


async def test_other_connack_refusals_are_connection_errors(pki: Pki) -> None:
    async with broker(pki, connack=3) as server:
        with pytest.raises(CloudConnectionError) as err:
            await aiomqtt_connect(options(pki, server.port))
    assert not isinstance(err.value, CloudAuthError)


async def test_rejected_client_certificate_is_an_auth_error(pki: Pki) -> None:
    # Blocking TLS server: unlike asyncio, it flushes the alert before closing.
    server = Broker(pki, trust=make_pki())
    context = server.context()
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        connection, _ = listener.accept()
        with contextlib.suppress(OSError), context.wrap_socket(connection, server_side=True):
            pass
        connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(CloudAuthError, match="refused this identity"):
            await aiomqtt_connect(options(pki, port))
    finally:
        thread.join(2)
        listener.close()


async def test_refused_own_subscription_is_an_auth_error(pki: Pki) -> None:
    async with broker(pki, refuse=(RECV_TOPIC,)) as server:
        with pytest.raises(CloudAuthError, match="own topic"):
            await aiomqtt_connect(options(pki, server.port))


def test_auth_failure_classification() -> None:
    class Coded(Exception):
        def __init__(self, rc: object) -> None:
            super().__init__("refused")
            self.rc = rc

    class Reason:
        value = 0x87

    assert is_auth_failure(Coded(5))
    assert is_auth_failure(Coded(Reason()))
    assert not is_auth_failure(Coded(3))
    assert is_auth_failure(OSError("[SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca"))
    assert is_auth_failure(OSError("[SSL: SSLV3_ALERT_CERTIFICATE_REVOKED] revoked"))
    assert not is_auth_failure(
        OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    )
    assert not is_auth_failure(OSError("Connection refused"))


async def test_refused_broadcast_subscription_is_only_reported(pki: Pki) -> None:
    async with broker(pki, refuse=(BROADCAST_TOPIC,)) as server:
        connection = await aiomqtt_connect(options(pki, server.port))
        await connection.close()
    assert dict(connection.granted) == {RECV_TOPIC: True, BROADCAST_TOPIC: False}


async def test_unverified_broker_certificate_is_rejected(pki: Pki) -> None:
    async with broker(pki) as server:
        with pytest.raises(CloudConnectionError):
            await aiomqtt_connect(options(pki, server.port, trusted=False))
