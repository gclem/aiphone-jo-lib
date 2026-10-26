# Contributing

Contributions are welcome: bug reports, documentation and pull requests.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.12 or later.

```bash
uv sync
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy
uv run pytest --cov
```

## Guidelines

- Keep changes focused; add or update tests for every behaviour change.
- Code must pass `ruff`, `ruff format` and `mypy --strict`.
- Write code, comments and documentation in English.
- Describe user-visible changes under `## [Unreleased]` in `CHANGELOG.md`.
- Tests must not contact real hardware or the Aiphone cloud. Use the loopback
  servers and fake transports in `tests/`.
- Never commit real identifiers or secrets: no client ids, topics, IP or MAC
  addresses, certificates, keys, codes or logs containing them. Use
  documentation values (RFC 5737 addresses such as `192.0.2.x`, MAC
  `00-00-5e-00-53-xx`, `example.com`).

## Reporting bugs

Open an issue with the library version, Python version, what you expected and
what happened. Redact logs before sharing them (the library already masks
secrets and identifiers in its own log messages). Report security issues as
described in [SECURITY.md](SECURITY.md).
