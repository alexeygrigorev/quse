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
same normalized record shape as JSON:

```json
{"provider":"claude","status":"ok","short_term":{"percent_remaining":55.0,"reset_at":"2026-05-24T14:30:00Z"},"long_term":{"percent_remaining":87.0,"reset_at":"2026-05-28T14:59:59Z"}}
```

Supported providers:

- `codex`
- `claude`
- `copilot`
- `zai`

`gemini` is accepted and reports `unsupported` because it does not currently
expose a usage endpoint.

Provider mapping:

- `codex`: `short_term` is hardcoded to `100%` remaining, `long_term` maps to
  the weekly API window.
- `claude`: `short_term` maps to the 5-hour signal, `long_term` maps to the
  7-day signal.
- `copilot`: `short_term` is hardcoded to `100%` remaining, `long_term` maps to
  the monthly premium-interactions signal.
- `zai`: `short_term` maps to the tokens signal, `long_term` is hardcoded to
  `100%` remaining.

## Release

Releases are published by GitHub Actions when a tag starting with `v` is pushed:

```bash
git tag v0.0.4
git push origin main --tags
```

The workflow verifies that the tag version matches `pyproject.toml`, runs the
tests, builds the wheel and sdist, then publishes to PyPI with the repository
secret `PYPI_API_TOKEN`.
