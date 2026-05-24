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

