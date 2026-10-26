"""Shared fixtures: throwaway certificates, credentials and a fake MQTT transport."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from aiphone_jo import CloudCredentials
from aiphone_jo.transport import MqttConnectOptions

FIXTURES = Path(__file__).parent / "fixtures"
CLIENT_ID = "client-abc"
MONITOR_ID = "00005e005301"  # fictitious MAC (RFC 7042 documentation range)
RECV_TOPIC = f"{MONITOR_ID}/1/0001/3/{CLIENT_ID}"
BROADCAST_TOPIC = f"{MONITOR_ID}/1/0001/3"
SEND_TOPIC = f"{MONITOR_ID}/1/0001/send/{CLIENT_ID}"


@dataclass(frozen=True)
class Pki:
    cert_pem: str
    key_pem: str
    der: bytes


def make_pki(*, days: int = 2, common_name: str = "localhost") -> Pki:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return Pki(
        cert_pem=certificate.public_bytes(serialization.Encoding.PEM).decode(),
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        der=certificate.public_bytes(serialization.Encoding.DER),
    )


@pytest.fixture(scope="session")
def pki() -> Pki:
    return make_pki()


@pytest.fixture(scope="session")
def replies() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "protocol_replies.json").read_text("utf-8"))
    return data


def credentials_input(pki: Pki) -> dict[str, Any]:
    return {
        "server_url": "example-ats.iot.eu-west-1.amazonaws.com",
        "client_id": CLIENT_ID,
        "recv_topic": RECV_TOPIC,
        "send_topic": SEND_TOPIC,
        "client_certificate": pki.cert_pem,
        "secret_key": pki.key_pem,
    }


@pytest.fixture
def credentials(pki: Pki) -> CloudCredentials:
    return CloudCredentials.parse({**credentials_input(pki), "term_name": "Home"})


Responder = Callable[[dict[str, Any]], list[dict[str, Any]]]


def reply_to(message: dict[str, Any], mid: str, rslt: int = 200, **extra: Any) -> dict[str, Any]:
    header = message["HEADER"]
    reply_header = {"MID": mid, "CLIID": "station-1", "RSLT": rslt}
    if "CID" in header:
        reply_header["CID"] = header["CID"]
    reply_header.update(extra.pop("header", {}))
    result: dict[str, Any] = {"HEADER": reply_header}
    if extra:
        result["BODY"] = extra
    return result


JANUS_OFFER = "v=0\r\ns=VideoRoom 1\r\na=ice-lite\r\n"


def default_responder(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Answers every command the client sends like a JO-1MDW does."""
    header = message["HEADER"]
    mid = header["MID"]
    if mid == "30001":
        return [reply_to(message, "31001", header={"CLIID": "relay-1"})]
    if mid == "23021":
        return [reply_to(message, "24021", header={"CLIID": "station-1"})]
    if mid in ("26021", "26121"):
        return [reply_to(message, "27" + mid[2:], rslt=0, REASON=0)]
    if mid == "26031":
        return [reply_to(message, "27031", rslt=0, ENTNR=2, KEYINF=[{"KEYTP": 2, "KEYSTS": 0}] * 2)]
    if mid == "30031":
        return [reply_to(message, "31031")]
    if mid == "23003":
        return [reply_to(message, "24003")]
    if mid == "30011":
        return [reply_to(message, "31011", SDP=JANUS_OFFER)]
    if mid == "30012":
        return [reply_to(message, "31012")]
    if mid == "30021":
        return [reply_to(message, "31021", SDP="v=0\r\nanswer\r\n")]
    if mid == "24002":
        return [reply_to(message, "25001")]
    if mid == "23008" and header["RSLT"] == 200:
        return [reply_to(message, "24008")]
    return []


class FakeConnection:
    """In-memory :class:`aiphone_jo.transport.MqttConnection`."""

    def __init__(self, responder: Responder, granted: dict[str, bool]) -> None:
        self.responder = responder
        self._granted = granted
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.queue: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue()
        self.closed = False

    @property
    def granted(self) -> dict[str, bool]:
        return self._granted

    @property
    def mids(self) -> list[str]:
        return [message["HEADER"]["MID"] for _, message in self.published]

    def headers(self, mid: str) -> list[dict[str, Any]]:
        return [m["HEADER"] for _, m in self.published if m["HEADER"]["MID"] == mid]

    async def publish(self, topic: str, payload: bytes) -> None:
        message = json.loads(payload)
        self.published.append((topic, message))
        for reply in self.responder(copy.deepcopy(message)):
            self.inject(reply)

    def inject(self, message: dict[str, Any], topic: str = RECV_TOPIC) -> None:
        self.queue.put_nowait((topic, json.dumps(message).encode()))

    def inject_raw(self, payload: bytes, topic: str = RECV_TOPIC) -> None:
        self.queue.put_nowait((topic, payload))

    def drop(self) -> None:
        self.queue.put_nowait(None)

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        while (item := await self.queue.get()) is not None:
            yield item

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)


class FakeBroker:
    """Transport factory creating :class:`FakeConnection` objects."""

    def __init__(self) -> None:
        self.responder: Responder = default_responder
        self.granted_broadcast = True
        self.connections: list[FakeConnection] = []
        self.options: list[MqttConnectOptions] = []
        self.fail_next = 0
        self.auth_fail = False

    @property
    def connection(self) -> FakeConnection:
        return self.connections[-1]

    def _respond(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        # Late binding: tests may swap ``responder`` after connecting.
        return self.responder(message)

    async def __call__(self, options: MqttConnectOptions) -> FakeConnection:
        from aiphone_jo import CloudConnectionError

        self.options.append(options)
        if self.auth_fail:
            from aiphone_jo import CloudAuthError

            raise CloudAuthError("not authorized")
        if self.fail_next:
            self.fail_next -= 1
            raise CloudConnectionError("refused")
        granted = {options.topics[0]: True}
        for topic in options.topics[1:]:
            granted[topic] = self.granted_broadcast
        connection = FakeConnection(self._respond, granted)
        self.connections.append(connection)
        return connection


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


async def settle(rounds: int = 5) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)
