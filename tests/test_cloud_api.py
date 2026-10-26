"""HTTPS API contracts: /registClient, /getRoomInfo and /deleteClientInfo."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import aiohttp
import pytest

from aiphone_jo import CloudApiError, CloudAuthError, CloudCredentials, InvalidInputError
from aiphone_jo.cloud_api import (
    build_regist_body,
    build_room_info_body,
    camera_terminals,
    describe_ent_key_type,
    get_room_info,
    parse_regist_response,
    parse_room_info_response,
    regist_client,
    regist_url,
    room_info_url,
    unregister,
    unregister_url,
)

from .conftest import CLIENT_ID, RECV_TOPIC, SEND_TOPIC, Pki

NOW = datetime(2024, 3, 5, 10, 20, 30, 123000)


class FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def json(self, content_type: str | None = None) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Fake aiohttp session; ``routes`` maps a URL path to (status, payload)."""

    def __init__(self, status: int = 200, payload: Any = None) -> None:
        self.status = status
        self.payload = payload
        self.routes: dict[str, tuple[int, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    @property
    def paths(self) -> list[str]:
        return [call["url"].split("/", 3)[-1] for call in self.calls]

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        if self.error:
            raise self.error
        self.calls.append({"url": url, **kwargs, "json": json.loads(kwargs["data"])})
        status, payload = self.routes.get("/" + url.split("/", 3)[-1], (self.status, self.payload))
        return FakeResponse(status, payload)


def regist_payload(pki: Pki) -> dict[str, Any]:
    # URL/CERT/SECKEY flat in BODY.
    return {
        "BODY": {
            "CLIID": CLIENT_ID,
            "URL": "example-ats.iot.eu-west-1.amazonaws.com",
            "SRVURL": "api-r1.jo.aiphone-app.net",
            "SECKEY": pki.key_pem,
            "CERT": pki.cert_pem,
            "RCVTPC": RECV_TOPIC,
            "SNDTPC": SEND_TOPIC,
            "ALLRCVTPC": "ignored",
        }
    }


def test_regist_body_matches_static_contract() -> None:
    body = build_regist_body(otp="123456", system_version="00:00", now=NOW)
    assert body == {
        "BODY": {
            "OTPASS": "123456",
            "OSKIND": "1",
            "SID": "20240305102030123000",
            "SYS": "4",
            "SYSVER": "00:00",
        }
    }
    assert "CLIID" not in body["BODY"]
    assert "SYSVER" not in build_regist_body(otp="1", now=NOW)["BODY"]


def test_api_urls_are_restricted_to_aiphone_app_net() -> None:
    assert (
        regist_url("api-r1.jo.aiphone-app.net") == "https://api-r1.jo.aiphone-app.net/registClient"
    )
    assert room_info_url("api-r1.jo.aiphone-app.net").endswith("/getRoomInfo")
    for origin in ("evil.example", "https://api-r1.jo.aiphone-app.net", "a.aiphone-app.net/x"):
        with pytest.raises(InvalidInputError):
            regist_url(origin)


def test_parse_regist_response(pki: Pki) -> None:
    params = parse_regist_response(regist_payload(pki))
    assert params["client_id"] == CLIENT_ID
    assert params["send_topic"] == SEND_TOPIC
    with pytest.raises(CloudApiError, match="without BODY"):
        parse_regist_response({})
    with pytest.raises(CloudApiError, match="Incomplete") as err:
        parse_regist_response({"BODY": {"CLIID": "x", "CERT": pki.cert_pem}})
    assert "BEGIN" not in str(err.value)
    assert "string(" in str(err.value)


async def test_regist_client_posts_and_validates(pki: Pki) -> None:
    session = FakeSession(payload=regist_payload(pki))
    credentials = await regist_client(
        session,  # type: ignore[arg-type]
        otp="otp-1",
        origin="api-r1.jo.aiphone-app.net",
        system_version="00:00",
    )
    call = session.calls[0]
    assert call["url"] == "https://api-r1.jo.aiphone-app.net/registClient"
    assert call["headers"] == {"Content-Type": "application/json"}
    assert call["json"]["BODY"]["OTPASS"] == "otp-1"
    assert credentials.client_id == CLIENT_ID
    assert credentials.api_origin == "api-r1.jo.aiphone-app.net"
    assert credentials.system_version == "00:00"


async def test_regist_client_rejects_non_200_and_network_errors(pki: Pki) -> None:
    with pytest.raises(CloudApiError, match="HTTP 500") as err:
        await regist_client(FakeSession(500), otp="x", origin="a.aiphone-app.net")  # type: ignore[arg-type]
    assert err.value.status == 500
    broken = FakeSession()
    broken.error = aiohttp.ClientConnectionError("down")
    with pytest.raises(CloudApiError, match="down") as network:
        await regist_client(broken, otp="x", origin="a.aiphone-app.net")  # type: ignore[arg-type]
    assert network.value.kind == "network"
    # No identity exists yet: 403 is an API error, not an identity refusal.
    with pytest.raises(CloudApiError) as forbidden:
        await regist_client(FakeSession(403), otp="x", origin="a.aiphone-app.net")  # type: ignore[arg-type]
    assert not isinstance(forbidden.value, CloudAuthError)
    with pytest.raises(CloudApiError) as invalid:
        await regist_client(
            FakeSession(payload=ValueError("bad json")),  # type: ignore[arg-type]
            otp="x",
            origin="a.aiphone-app.net",
        )
    assert invalid.value.kind == "invalid_response"


def test_room_info_body(credentials: CloudCredentials) -> None:
    assert build_room_info_body(credentials, NOW) == {
        "BODY": {"CLIID": CLIENT_ID, "SYS": "4", "SID": "20240305102030123000"}
    }


def test_parse_room_info_mgmt_format() -> None:
    info = parse_room_info_response(
        {
            "BODY": {
                "MGMT": {
                    "ID": ["101", "102", "1"],
                    "KIND": ["5", "5", "1"],
                    "NAME": [["Entrance 1", ""], ["Entrance 2", ""], ["Monitor", ""]],
                    "CAMERA": ["1", "1", "0"],
                    "KEY": ["2", "2", ""],
                    "PROP": ["0", "0", "0"],
                    "PROCFLG": ["1", "1", "1"],
                },
                "SECSET": 0,
                "ALMSTS": 0,
            }
        }
    )
    assert [t.id for t in info.camera_terminals] == [101, 102]
    assert info.terminals[0].name == "Entrance 1"
    assert info.terminals[0].ent_key_type_label.startswith("unlockable")
    assert info.terminals[2].ent_key_type is None
    assert info.term_inf_num == 3
    assert (info.alm_sts, info.sec_alm_sp) == (0, 0)


def test_parse_room_info_term_inf_fallback() -> None:
    info = parse_room_info_response(
        {
            "BODY": {
                "TERM_INF_NUM": 2,
                "TERM_INF": {
                    "3": {"TERM_KIND": 5, "TERM_NAME": "Door", "CMR_STS": 1, "ENT_KY_TP": 1},
                    "4": {"TERM_KIND": 5, "TERM_NAME": "Door 2", "CMR_STS": 0},
                },
            }
        }
    )
    assert [t.id for t in info.terminals] == [3, 4]
    assert [t.id for t in camera_terminals(info.terminals)] == [3]
    assert info.terminals[0].ent_key_type_label.startswith("lockable")
    assert describe_ent_key_type(9) == "undocumented value (9)"
    assert describe_ent_key_type(None) == "unknown"
    assert camera_terminals(parse_room_info_response({"BODY": {}}).terminals) == ()
    with pytest.raises(CloudApiError):
        parse_room_info_response({"nope": 1})


async def test_get_room_info(credentials: CloudCredentials) -> None:
    session = FakeSession(payload={"BODY": {"MGMT": {"ID": ["7"], "KIND": ["5"], "CAMERA": ["1"]}}})
    info = await get_room_info(session, credentials)  # type: ignore[arg-type]
    assert session.calls[0]["url"] == "https://api-r1.jo.aiphone-app.net/getRoomInfo"
    assert session.calls[0]["json"]["BODY"]["CLIID"] == CLIENT_ID
    assert info.camera_terminals[0].id == 7
    with pytest.raises(CloudAuthError, match="HTTP 403") as err:
        await get_room_info(FakeSession(403), credentials)  # type: ignore[arg-type]
    assert err.value.status == 403
    with pytest.raises(CloudApiError, match="HTTP 500"):
        await get_room_info(FakeSession(500), credentials)  # type: ignore[arg-type]


async def test_unregister_request_matches_term_info_contract(
    credentials: CloudCredentials,
) -> None:
    session = FakeSession(payload={"BODY": {"CLIID": CLIENT_ID}})
    assert await unregister(session, credentials) is True  # type: ignore[arg-type]
    call = session.calls[0]
    assert call["url"] == "https://api-r1.jo.aiphone-app.net/deleteClientInfo"
    assert call["headers"] == {"Content-Type": "application/json"}
    body = call["json"]["BODY"]
    assert list(body) == ["CLIID", "SID", "SYS"]
    assert (body["CLIID"], body["SYS"]) == (CLIENT_ID, "4")
    assert unregister_url("api-r1.jo.aiphone-app.net").endswith("/deleteClientInfo")


async def test_unregister_is_idempotent(
    credentials: CloudCredentials, caplog: pytest.LogCaptureFixture
) -> None:
    for status in (404, 410):
        assert await unregister(FakeSession(status), credentials) is False  # type: ignore[arg-type]
    assert await unregister(FakeSession(payload={"BODY": {}}), credentials) is True  # type: ignore[arg-type]
    assert "without BODY.CLIID" in caplog.text


@pytest.mark.parametrize("status", [401, 403])
async def test_unregister_identity_refused(credentials: CloudCredentials, status: int) -> None:
    with pytest.raises(CloudAuthError) as err:
        await unregister(FakeSession(status), credentials)  # type: ignore[arg-type]
    assert err.value.status == status


async def test_unregister_errors(credentials: CloudCredentials) -> None:
    with pytest.raises(CloudApiError) as err:
        await unregister(FakeSession(500), credentials)  # type: ignore[arg-type]
    assert (err.value.status, err.value.kind) == (500, "http")
    broken = FakeSession()
    broken.error = TimeoutError()
    with pytest.raises(CloudApiError) as timeout:
        await unregister(broken, credentials)  # type: ignore[arg-type]
    assert timeout.value.kind == "network"
