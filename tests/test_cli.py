"""Command line tool (every network call is replaced)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from aiphone_jo import (
    CloudCredentials,
    DiscoveredMonitor,
    InvalidInputError,
    NoMonitorFoundError,
    PairingFailureReason,
    PairingRejectedError,
    PairingStage,
    cli,
)

from .conftest import FakeBroker, Pki, credentials_input


def test_open_help_hides_deprecated_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["open", "--help"])
    help_text = capsys.readouterr().out
    assert "--output" in help_text
    assert "--big" not in help_text
    assert "--small" not in help_text


def test_parser_requires_an_output_for_open() -> None:
    parser = cli.build_parser()
    for argv in (
        ["open", "--terminal", "102"],
        ["open", "--output", "3", "--terminal", "102"],
        ["open", "--output", "1", "--small", "--terminal", "102"],
        ["open", "--big", "--small", "--terminal", "102"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    args = parser.parse_args(["--yes", "open", "--output", "2", "--terminal", "102"])
    assert (args.output, args.terminal, args.yes) == (2, 102, True)


def test_state_file_is_private_and_round_trips(
    tmp_path: Path, credentials: CloudCredentials
) -> None:
    path = tmp_path / "nested" / "credentials.json"
    cli.save_state(path, credentials, {"address": "192.0.2.46", "port": 50000})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    loaded, monitor = cli.load_state(path)
    assert loaded == credentials
    assert monitor["port"] == 50000


def test_missing_state_is_a_clean_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--credentials", str(tmp_path / "none.json"), "status"]) == 1
    assert "run 'aiphone-jo pair'" in capsys.readouterr().err


def test_discover_prints_monitors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_discover(targets: Any, **kwargs: Any) -> list[DiscoveredMonitor]:
        assert targets == ["192.0.2.255"]
        return [DiscoveredMonitor("192.0.2.46", 50000, 4, "00:00")]

    monkeypatch.setattr(cli, "discover", fake_discover)
    assert cli.main(["discover", "--target", "192.0.2.255"]) == 0
    assert "192.0.2.46:50000 (SYS 4, SYSVER 00:00)" in capsys.readouterr().out

    async def nothing(targets: Any, **kwargs: Any) -> list[DiscoveredMonitor]:
        raise NoMonitorFoundError("No monitor found.")

    monkeypatch.setattr(cli, "discover", nothing)
    assert cli.main(["discover", "--target", "192.0.2.255"]) == 1


def _patch_client(monkeypatch: pytest.MonkeyPatch, broker: FakeBroker) -> None:
    from aiphone_jo.client import AiphoneJoClient as original

    def factory(credentials: CloudCredentials, **kwargs: Any) -> Any:
        return original(credentials, transport_factory=broker, **kwargs)

    monkeypatch.setattr(cli, "AiphoneJoClient", factory)


def test_open_requires_typed_confirmation(
    tmp_path: Path,
    credentials: CloudCredentials,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "credentials.json"
    cli.save_state(path, credentials, {})
    _patch_client(monkeypatch, broker)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    argv = ["--credentials", str(path), "open", "--terminal", "102", "--output"]
    assert cli.main([*argv, "1"]) == 1
    assert "Cancelled" in capsys.readouterr().err
    assert broker.connections == []
    monkeypatch.setattr("builtins.input", lambda prompt: cli.OPEN_CONFIRMATION)
    assert cli.main([*argv, "2"]) == 0
    assert "output 2 (option output)" in capsys.readouterr().out
    assert broker.connection.mids == ["30001", "23021", "26121", "30031", "23003"]
    assert cli.main([*argv, "1"]) == 0
    assert broker.connection.mids[-3] == "26021"


@pytest.mark.parametrize(
    ("option", "mid", "replacement"),
    [("--big", "26021", "--output 1"), ("--small", "26121", "--output 2")],
)
def test_deprecated_open_options_still_work(
    tmp_path: Path,
    credentials: CloudCredentials,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    option: str,
    mid: str,
    replacement: str,
) -> None:
    path = tmp_path / "credentials.json"
    cli.save_state(path, credentials, {})
    _patch_client(monkeypatch, broker)
    assert cli.main(["--credentials", str(path), "--yes", "open", option, "--terminal", "102"]) == 0
    assert broker.connection.mids[2] == mid
    assert f"{option} is deprecated, use {replacement}" in capsys.readouterr().err


def test_status_and_ring_watch(
    tmp_path: Path,
    credentials: CloudCredentials,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "credentials.json"
    cli.save_state(path, credentials, {})
    _patch_client(monkeypatch, broker)
    assert cli.main(["--credentials", str(path), "status"]) == 0
    out = capsys.readouterr().out
    assert "Entrances: 2" in out
    assert "KEYTP=2 KEYSTS=0" in out
    assert cli.main(["--credentials", str(path), "ring-watch", "--duration", "0.01"]) == 0
    assert "Waiting for rings" in capsys.readouterr().out


def test_pair_refuses_to_overwrite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "credentials.json"
    path.write_text("{}")
    assert cli.main(["--credentials", str(path), "pair", "--target", "192.0.2.46"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_unregister_command(
    tmp_path: Path,
    credentials: CloudCredentials,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "credentials.json"
    cli.save_state(path, credentials, {})
    calls: list[str] = []

    async def fake_unregister(session: Any, creds: CloudCredentials) -> bool:
        calls.append(creds.client_id)
        return False

    monkeypatch.setattr(cli, "unregister", fake_unregister)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert cli.main(["--credentials", str(path), "unregister"]) == 1
    assert calls == []
    monkeypatch.setattr("builtins.input", lambda prompt: cli.UNREGISTER_CONFIRMATION)
    assert cli.main(["--credentials", str(path), "unregister", "--keep-file"]) == 0
    assert path.exists()
    assert cli.main(["--credentials", str(path), "unregister"]) == 0
    assert not path.exists()
    assert calls == [credentials.client_id] * 2
    assert "already deleted" in capsys.readouterr().out


def test_legacy_identity_file_is_migrated(tmp_path: Path, pki: Pki) -> None:
    path = tmp_path / "credentials.json"
    legacy = {**credentials_input(pki), "term_name": "Home"}
    path.write_text(json.dumps({"credentials": legacy, "monitor": {"port": 1}}), "utf-8")
    loaded, monitor = cli.load_state(path)
    assert (loaded.term_name, monitor) == ("Home", {"port": 1})
    path.write_text("[]", "utf-8")
    with pytest.raises(InvalidInputError, match="Unreadable"):
        cli.load_state(path)


def test_pairing_errors_show_their_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    async def refused(targets: Any, **kwargs: Any) -> list[DiscoveredMonitor]:
        raise PairingRejectedError(
            "The monitor refused the discovery (RSLT=2).",
            stage=PairingStage.DISCOVERY,
            reason=PairingFailureReason.REGISTRATION_IN_PROGRESS,
            mid="11001",
            rslt=2,
        )

    monkeypatch.setattr(cli, "discover", refused)
    path = tmp_path / "credentials.json"
    assert cli.main(["--credentials", str(path), "pair", "--target", "192.0.2.46"]) == 1
    err = capsys.readouterr().err
    assert "reason=registration_in_progress" in err
    assert "mid=11001" in err
    assert "rslt=2" in err
