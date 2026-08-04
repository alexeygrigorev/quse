# quse

Quota and usage checks for coding-agent CLIs.

`quse` reports normalized usage for providers used by tools such as Codex,
Claude Code, GitHub Copilot, and Z.AI/goz.

```bash
quse
quse codex
quse copilot --json
```

The CLI prints one normalized line per provider by default. `--json` emits the
same normalized records as JSON keyed by provider name:

```json
{
  "claude": {
    "details": {},
    "error": null,
    "long_term": {
      "percent_remaining": 87.0,
      "reset_at": "2026-05-28T14:59:59Z"
    },
    "short_term": {
      "percent_remaining": 55.0,
      "reset_at": "2026-05-24T14:30:00Z"
    },
    "status": "ok"
  }
}
```

Supported providers:

- `codex`
- `claude`
- `zai`
- `copilot`

`gemini` is accepted and reports `unsupported` because it does not currently
expose a usage endpoint.

Provider mapping:

- `codex`: two API windows map to the 5-hour `short_term` window and weekly
  `long_term` window; when the API returns one window, it is weekly only.
  Codex JSON output also includes `details.reset_credits` from ChatGPT's
  rate-limit reset-credit endpoint when available.
- `claude`: `short_term` maps to the 5-hour signal, `long_term` maps to the
  7-day signal.
- `copilot`: `short_term` is hardcoded to `100%` remaining, `long_term` maps to
  the monthly premium-interactions signal.
- `zai`: `short_term` maps to the 5-hour quota, `long_term` maps to the
  weekly quota.

## Install

For a local checkout, install the project environment and add its `.venv/bin`
to your shell PATH:

```bash
uv sync --dev
./install.sh
```

Open a new shell after running `./install.sh`, then verify:

```bash
quse --help
```

This is the same local-checkout style used by `tmuxctl`: the script appends
the checkout's `.venv/bin` directory to `~/.bashrc`.

For one-off use from a checkout without changing PATH:

```bash
uv run quse
uv run quse codex --json
```

For the latest released package from PyPI:

```bash
uvx quse
uv tool install quse
# or
pipx install quse
```

Use `uvx quse` for a one-off run without installing a persistent tool.

## Release

Releases are published by GitHub Actions when a tag starting with `v` is pushed:

```bash
git tag v0.0.6
git push origin main --tags
```

The workflow verifies that the tag version matches `pyproject.toml`, runs the
tests, builds the wheel and sdist, then publishes to PyPI with the repository
secret `PYPI_API_TOKEN`.
