"""Stable pairing failure reasons."""

from __future__ import annotations

import pytest

from aiphone_jo import (
    APP_CODE_INCOMPATIBLE,
    APP_CODE_REGIST_CLIENT,
    PAIRING_HINTS,
    PAIRING_RSLT_REASONS,
    RETRY_ON_SAME_CONNECTION,
    CloudApiError,
    DiscoveryError,
    NoMonitorFoundError,
    PairingCloudError,
    PairingError,
    PairingFailureReason,
    PairingStage,
    PairingTimeoutError,
    ResponseTimeoutError,
    UntrustedPeerError,
    pairing_reason,
)

R = PairingFailureReason


@pytest.mark.parametrize(
    ("mid", "rslt", "reason"),
    [
        ("11001", 1, R.RETRY),
        ("11001", 2, R.REGISTRATION_IN_PROGRESS),
        ("11001", 3, R.TERMINAL_LIMIT_REACHED),
        ("11003", 1, R.WRONG_CODE),
        ("11003", 2, R.WRONG_CODE),
        ("11003", 3, R.CODE_EXPIRED),
        ("11005", 1, R.RETRY),
        ("11005", 2, R.NAME_TOO_LONG),
        ("11005", 3, R.NAME_ALREADY_USED),
        ("11005", 4, R.EULA_NOT_ACCEPTED),
        ("70002", 1, R.INCOMPATIBLE_VERSION),
        ("71001", 451, R.EULA_NOT_ACCEPTED),
        ("11005", 99, R.UNKNOWN_RESULT),
        ("11003", "2", R.UNKNOWN_RESULT),
        (None, 1, R.UNKNOWN_RESULT),
    ],
)
def test_reason_table(mid: str | None, rslt: object, reason: PairingFailureReason) -> None:
    assert pairing_reason(mid, rslt) is reason


def test_reason_values_are_stable_strings() -> None:
    # These values are part of the public API (UI translations, HA errors).
    assert R.WRONG_CODE.value == "wrong_code"
    assert R.NAME_ALREADY_USED.value == "name_already_used"
    assert R.LINK_CLOSED.value == "link_closed"
    assert (APP_CODE_INCOMPATIBLE, APP_CODE_REGIST_CLIENT) == ("70002", "71001")
    assert set(PAIRING_HINTS) == set(R)
    assert set(PAIRING_RSLT_REASONS.values()) <= set(R)
    assert {("11003", 2), ("11005", 2), ("11005", 3)} == RETRY_ON_SAME_CONNECTION
    with pytest.raises(TypeError):
        PAIRING_RSLT_REASONS[("x", 1)] = R.RETRY  # type: ignore[index]


def test_error_fields_and_hierarchy() -> None:
    error = PairingError(
        "x",
        stage=PairingStage.AUTHENTICATION,
        reason=R.WRONG_CODE,
        mid="11003",
        rslt=2,
        retry_same_connection=True,
    )
    assert (error.stage, error.reason, error.mid, error.rslt) == (
        PairingStage.AUTHENTICATION,
        R.WRONG_CODE,
        "11003",
        2,
    )
    assert error.hint == "authentication failed: wrong code"
    assert error.retry_same_connection
    missing = NoMonitorFoundError("none")
    assert isinstance(missing, DiscoveryError)
    assert isinstance(missing, PairingError)
    assert (missing.stage, missing.reason) == (PairingStage.DISCOVERY, R.MONITOR_NOT_FOUND)
    timeout = PairingTimeoutError("late", mid="11002")
    assert isinstance(timeout, ResponseTimeoutError)
    assert (timeout.reason, timeout.mid) == (R.TIMEOUT, "11002")
    peer = UntrustedPeerError("bad", peer="p")
    assert (peer.reason, peer.stage, peer.peer) == (R.FINGERPRINT_MISMATCH, PairingStage.TRUST, "p")
    cloud = PairingCloudError("451", status=451, reason=R.EULA_NOT_ACCEPTED, mid="71001", rslt=451)
    assert isinstance(cloud, CloudApiError)
    assert (cloud.status, cloud.kind, cloud.stage) == (451, "http", PairingStage.CLOUD_REGISTRATION)
