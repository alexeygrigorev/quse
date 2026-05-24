# quse

Quota and usage checks for coding-agent CLIs.

`quse` reports normalized usage for providers used by tools such as Codex,
Claude Code, GitHub Copilot, and Z.AI/goz.

```bash
quse
quse codex
quse copilot --json
```

The CLI prints one line per provider by default. `--json` emits the same record
shape as JSON.

Supported providers:

- `codex`
- `claude`
- `copilot`
- `zai`

`gemini` is accepted and reports `unsupported` because it does not currently
expose a usage endpoint.

## Release

Releases are published by GitHub Actions when a tag starting with `v` is pushed:

```bash
git tag v0.0.3
git push origin main --tags
```

The workflow verifies that the tag version matches `pyproject.toml`, runs the
tests, builds the wheel and sdist, then publishes to PyPI with the repository
secret `PYPI_API_TOKEN`.
