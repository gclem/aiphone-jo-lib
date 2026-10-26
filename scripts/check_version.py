"""Release consistency checks for aiphone-jo.

The version has a single source: ``__version__`` in
``src/aiphone_jo/__init__.py`` (``pyproject.toml`` reads it dynamically).
This script verifies that:

* ``__version__`` is a plain release version (``X.Y.Z``, optionally
  ``aN``/``bN``/``rcN``) and equals the installed package metadata;
* ``CHANGELOG.md`` keeps an ``## [Unreleased]`` section and never lists a
  released version newer than ``__version__``;
* with ``--tag``: the tag is exactly ``v<__version__>`` and the changelog has a
  dated ``## [X.Y.Z] - YYYY-MM-DD`` section with notes (written to
  ``--notes-out`` for the GitHub Release);
* with ``--dist``: the wheel and sdist for this version were built.

Exit code 0 when everything is consistent, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "src" / "aiphone_jo" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
DISTRIBUTION = "aiphone-jo"

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
HEADING_RE = re.compile(r"^## \[(?P<name>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$")


class ReleaseCheckError(Exception):
    """A release consistency rule is violated."""


@dataclass(frozen=True)
class Section:
    """One ``## [...]`` section of the changelog."""

    name: str
    date: str | None
    body: str


def read_version(path: Path = INIT) -> str:
    """Return the ``__version__`` string literal of ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__version__" for t in statement.targets)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            return statement.value.value
    raise ReleaseCheckError(f"No __version__ string literal in {path}.")


def version_key(version: str) -> tuple[int, int, int, int, int]:
    """Sortable key; pre-releases sort before the final release."""
    match = VERSION_RE.match(version)
    if not match:
        raise ReleaseCheckError(
            f"Invalid version {version!r}: expected X.Y.Z (optionally aN, bN or rcN)."
        )
    major, minor, patch, kind, number = match.groups()
    rank = {"a": 0, "b": 1, "rc": 2, None: 3}[kind]
    return int(major), int(minor), int(patch), rank, int(number or 0)


def is_prerelease(version: str) -> bool:
    """``True`` for ``aN``/``bN``/``rcN`` versions."""
    return version_key(version)[3] < 3


def parse_changelog(text: str) -> list[Section]:
    """Split the changelog into ``## [...]`` sections."""
    sections: list[Section] = []
    name: str | None = None
    date: str | None = None
    lines: list[str] = []
    for line in [*text.splitlines(), "## [__end__]"]:
        if line.startswith("## "):
            match = HEADING_RE.match(line)
            if not match:
                raise ReleaseCheckError(f"Invalid changelog heading: {line!r}.")
            if name is not None:
                sections.append(Section(name, date, "\n".join(lines).strip()))
            name, date, lines = match["name"], match["date"], []
        elif name is not None:
            lines.append(line)
    return sections


def check(
    version: str,
    changelog: str,
    *,
    tag: str | None = None,
    installed: str | None = None,
) -> Section | None:
    """Validate the rules above; return the release section in tag mode."""
    version_key(version)
    if installed is not None and installed != version:
        raise ReleaseCheckError(
            f"Installed metadata version {installed} differs from __version__ {version}: "
            "run `uv sync`."
        )
    sections = parse_changelog(changelog)
    names = [section.name for section in sections]
    if "Unreleased" not in names:
        raise ReleaseCheckError("CHANGELOG.md must keep an '## [Unreleased]' section.")
    if len(names) != len(set(names)):
        raise ReleaseCheckError("CHANGELOG.md has duplicate sections.")
    released = [s for s in sections if s.name != "Unreleased"]
    for section in released:
        if section.date is None:
            raise ReleaseCheckError(f"Section [{section.name}] needs a ' - YYYY-MM-DD' date.")
        if version_key(section.name) > version_key(version):
            raise ReleaseCheckError(
                f"CHANGELOG.md lists {section.name}, newer than __version__ {version}."
            )
    if tag is None:
        return None
    if tag != f"v{version}":
        raise ReleaseCheckError(
            f"Tag {tag} does not match __version__ {version} (expected v{version})."
        )
    current = next((s for s in released if s.name == version), None)
    if current is None:
        raise ReleaseCheckError(f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' section.")
    if not current.body:
        raise ReleaseCheckError(f"CHANGELOG.md section [{version}] is empty.")
    return current


def check_dist(version: str, dist: Path) -> list[Path]:
    """Return the wheel and sdist built for ``version`` (error if missing)."""
    expected = [
        dist / f"aiphone_jo-{version}-py3-none-any.whl",
        dist / f"aiphone_jo-{version}.tar.gz",
    ]
    missing = [path.name for path in expected if not path.is_file()]
    if missing:
        raise ReleaseCheckError(f"Missing build artifacts in {dist}: {', '.join(missing)}.")
    return expected


def _installed_version() -> str | None:
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", help="release tag to validate (vX.Y.Z)")
    parser.add_argument("--notes-out", type=Path, help="write the release notes here")
    parser.add_argument("--dist", type=Path, help="check the built wheel and sdist")
    parser.add_argument("--github-output", type=Path, help="append version outputs (Actions)")
    args = parser.parse_args(argv)
    try:
        version = read_version()
        section = check(
            version,
            CHANGELOG.read_text(encoding="utf-8"),
            tag=args.tag,
            installed=_installed_version(),
        )
        if args.dist is not None:
            check_dist(version, args.dist)
    except ReleaseCheckError as err:
        sys.stderr.write(f"error: {err}\n")
        return 1
    if args.notes_out is not None and section is not None:
        args.notes_out.write_text(f"{section.body}\n", encoding="utf-8")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"version={version}\nprerelease={str(is_prerelease(version)).lower()}\n")
    sys.stdout.write(f"aiphone-jo {version}: release metadata consistent.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
