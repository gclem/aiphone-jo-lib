"""Credential validation and serialization."""

from __future__ import annotations

import json
import re
import ssl
from datetime import UTC, datetime, timedelta

import pytest

from aiphone_jo import (
    STORAGE_SCHEMA,
    CloudCredentials,
    CredentialsExpiredError,
    InvalidCredentialsError,
    broadcast_topic_of,
)

from .conftest import BROADCAST_TOPIC, CLIENT_ID, MONITOR_ID, Pki, credentials_input, make_pki


def test_valid_identity(pki: Pki) -> None:
    credentials = CloudCredentials.parse(credentials_input(pki))
    assert credentials.recv_topic.endswith(CLIENT_ID)
    assert credentials.broadcast_topic == BROADCAST_TOPIC
    assert credentials.expires is not None
    assert credentials.expires > datetime.now(UTC)
    assert "BEGIN" not in repr(credentials)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"secret_key": "cert"}, "PEM|Certificate/key"),
        ({"recv_topic": "#"}, "wildcard"),
        ({"recv_topic": "a/+/b"}, "wildcard"),
        ({"server_url": "evil.example"}, "server_url"),
        ({"server_url": "https://x.iot.eu-west-1.amazonaws.com"}, "server_url"),
        ({"port": 80}, "port"),
        ({"client_certificate": "bad"}, "PEM"),
        ({"alpn": "h2"}, "ALPN"),
        ({"client_id": ""}, "client_id"),
    ],
)
def test_invalid_identity(pki: Pki, change: dict[str, object], match: str) -> None:
    data = credentials_input(pki)
    for key, value in change.items():
        data[key] = pki.cert_pem if value == "cert" else value
    with pytest.raises(InvalidCredentialsError, match=match):
        CloudCredentials.parse(data)


def test_key_must_match_certificate(pki: Pki) -> None:
    other = make_pki()
    with pytest.raises(InvalidCredentialsError, match="does not match"):
        CloudCredentials.parse({**credentials_input(pki), "secret_key": other.key_pem})


def test_expired_certificate(pki: Pki) -> None:
    later = datetime.now(UTC) + timedelta(days=10)
    with pytest.raises(CredentialsExpiredError, match="expired"):
        CloudCredentials.parse(credentials_input(pki), now=later)
    lenient = CloudCredentials.parse(credentials_input(pki), check_validity=False, now=later)
    assert lenient.expires is not None
    assert lenient.is_expired(later)
    assert not lenient.is_expired()


def test_not_yet_valid_is_not_reported_as_expired(pki: Pki) -> None:
    earlier = datetime.now(UTC) - timedelta(days=1)
    with pytest.raises(InvalidCredentialsError, match="not yet valid") as err:
        CloudCredentials.parse(credentials_input(pki), now=earlier)
    assert not isinstance(err.value, CredentialsExpiredError)


def test_storage_round_trip(pki: Pki) -> None:
    credentials = CloudCredentials.parse({**credentials_input(pki), "term_name": "Home"})
    stored = credentials.to_storage()
    assert stored["schema"] == STORAGE_SCHEMA == 1
    assert stored["expires"] == credentials.expires.isoformat()  # type: ignore[union-attr]
    assert json.loads(json.dumps(stored)) == stored
    assert CloudCredentials.from_storage(stored) == credentials


def test_storage_tolerates_unknown_keys_and_migrates_legacy(pki: Pki) -> None:
    credentials = CloudCredentials.parse({**credentials_input(pki), "term_name": "Home"})
    stored = {**credentials.to_storage(), "future_field": 1, "expires": "garbage"}
    assert CloudCredentials.from_storage(stored) == credentials
    legacy = {**credentials_input(pki), "term_name": "Home", "api_origin": None, "extra": True}
    assert CloudCredentials.from_storage(legacy) == credentials
    with pytest.warns(DeprecationWarning, match="to_storage"):
        old = credentials.as_dict()
    assert CloudCredentials.from_storage(old) == credentials


def test_storage_rejects_unknown_schema_and_expired(pki: Pki) -> None:
    credentials = CloudCredentials.parse(credentials_input(pki))
    with pytest.raises(InvalidCredentialsError, match="newer"):
        CloudCredentials.from_storage({**credentials.to_storage(), "schema": 99})
    for schema in ("1", -1, True):
        with pytest.raises(InvalidCredentialsError, match="schema"):
            CloudCredentials.from_storage({**credentials.to_storage(), "schema": schema})
    with pytest.raises(InvalidCredentialsError):
        CloudCredentials.from_storage([])  # type: ignore[arg-type]
    later = datetime.now(UTC) + timedelta(days=10)
    with pytest.raises(CredentialsExpiredError):
        CloudCredentials.from_storage(credentials.to_storage(), now=later)


def test_monitor_id_is_stable_and_hides_the_topic(pki: Pki) -> None:
    first = CloudCredentials.parse(credentials_input(pki))
    other_terminal = CloudCredentials.parse(
        {
            **credentials_input(pki),
            "client_id": "client-xyz",
            "recv_topic": f"{BROADCAST_TOPIC}/client-xyz",
        }
    )
    assert first.monitor_id == other_terminal.monitor_id
    assert first.monitor_id is not None
    assert re.fullmatch(r"jo-[0-9a-f]{24}", first.monitor_id)
    assert MONITOR_ID not in first.monitor_id
    other_monitor = CloudCredentials.parse(
        {**credentials_input(pki), "recv_topic": f"00005e005302/1/0001/3/{CLIENT_ID}"}
    )
    assert other_monitor.monitor_id != first.monitor_id
    no_suffix = CloudCredentials.parse({**credentials_input(pki), "recv_topic": "own/topic"})
    assert no_suffix.monitor_id is None


def test_with_terminal_name(credentials: CloudCredentials) -> None:
    assert credentials.with_terminal_name("Hall").term_name == "Hall"
    with pytest.raises(InvalidCredentialsError):
        credentials.with_terminal_name("x" * 13)


def test_broadcast_topic_of() -> None:
    assert (
        broadcast_topic_of("00005e005301/1/0001/3/client-1", "client-1") == "00005e005301/1/0001/3"
    )
    assert broadcast_topic_of("own/assigned/topic", "test") is None
    assert broadcast_topic_of("/client-1", "client-1") is None
    assert broadcast_topic_of(None, "x") is None


async def test_ssl_context_loads_client_chain(pki: Pki) -> None:
    credentials = CloudCredentials.parse(
        {**credentials_input(pki), "root_ca": pki.cert_pem, "port": 443, "alpn": "x-amzn-mqtt-ca"}
    )
    context = await credentials.async_create_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
