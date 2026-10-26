"""Release consistency checks (scripts/check_version.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

import aiphone_jo

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_version.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_version", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_version"] = module
    spec.loader.exec_module(module)
    return module


cv = _load()

CHANGELOG = """# Changelog

## [Unreleased]

## [1.2.0] - 2026-01-02

### Added

- Something.

## [1.1.0] - 2025-12-01

- Older.
"""


def test_repository_is_consistent() -> None:
    version = cv.read_version()
    assert version == aiphone_jo.__version__
    cv.check(version, cv.CHANGELOG.read_text("utf-8"), installed=aiphone_jo.__version__)
    assert cv.main([]) == 0


def test_version_rules() -> None:
    assert cv.version_key("1.2.3rc1") < cv.version_key("1.2.3")
    assert cv.version_key("1.10.0") > cv.version_key("1.9.9")
    assert cv.is_prerelease("2.0.0b1")
    assert not cv.is_prerelease("2.0.0")
    for bad in ("1.2", "v1.2.3", "1.2.3.dev1", "1.2.3-rc1"):
        with pytest.raises(cv.ReleaseCheckError, match="Invalid version"):
            cv.version_key(bad)


def test_tag_mode_returns_the_release_notes() -> None:
    section = cv.check("1.2.0", CHANGELOG, tag="v1.2.0", installed="1.2.0")
    assert section.date == "2026-01-02"
    assert section.body == "### Added\n\n- Something."


@pytest.mark.parametrize(
    ("version", "changelog", "tag", "installed", "message"),
    [
        ("1.2.0", CHANGELOG, "v1.2.1", None, "does not match"),
        ("1.2.0", CHANGELOG, "1.2.0", None, "does not match"),
        ("1.3.0", CHANGELOG, "v1.3.0", None, r"no '## \[1.3.0\]"),
        ("1.1.9", CHANGELOG, None, None, "newer than"),
        ("1.2.0", CHANGELOG, None, "1.1.0", "Installed metadata"),
        ("1.2.0", CHANGELOG.replace("## [Unreleased]\n", ""), None, None, "Unreleased"),
        ("1.2.0", CHANGELOG.replace(" - 2025-12-01", ""), None, None, "needs a"),
        ("1.2.0", CHANGELOG + "\n## [1.1.0] - 2025-12-01\n", None, None, "duplicate"),
        ("1.2.0", CHANGELOG.replace("## [1.1.0]", "## 1.1.0"), None, None, "Invalid changelog"),
        (
            "1.2.0",
            CHANGELOG.replace("- Something.", "").replace("### Added", ""),
            "v1.2.0",
            None,
            "empty",
        ),
    ],
)
def test_inconsistencies_are_rejected(
    version: str, changelog: str, tag: str | None, installed: str | None, message: str
) -> None:
    with pytest.raises(cv.ReleaseCheckError, match=message):
        cv.check(version, changelog, tag=tag, installed=installed)


def test_unreleased_version_passes_outside_tag_mode() -> None:
    assert cv.check("1.3.0", CHANGELOG) is None


def test_dist_check(tmp_path: Path) -> None:
    with pytest.raises(cv.ReleaseCheckError, match="Missing build artifacts"):
        cv.check_dist("1.2.0", tmp_path)
    (tmp_path / "aiphone_jo-1.2.0-py3-none-any.whl").write_bytes(b"")
    (tmp_path / "aiphone_jo-1.2.0.tar.gz").write_bytes(b"")
    assert len(cv.check_dist("1.2.0", tmp_path)) == 2


def test_cli_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    version = aiphone_jo.__version__
    changelog.write_text(
        f"## [Unreleased]\n\n## [{version}] - 2026-01-02\n\n### Added\n\n- Something.\n", "utf-8"
    )
    monkeypatch.setattr(cv, "CHANGELOG", changelog)
    notes, output = tmp_path / "notes.md", tmp_path / "out"
    tag = f"v{aiphone_jo.__version__}"
    assert cv.main(["--tag", tag, "--notes-out", str(notes), "--github-output", str(output)]) == 0
    assert notes.read_text("utf-8") == "### Added\n\n- Something.\n"
    assert output.read_text("utf-8") == f"version={aiphone_jo.__version__}\nprerelease=false\n"
    assert cv.main(["--tag", "v0.0.0"]) == 1
    assert cv.main(["--dist", str(tmp_path / "missing")]) == 1
