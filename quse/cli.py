"""Command-line interface for quse."""

from __future__ import annotations

import json
from typing import Any

import click

from quse.usage import UnknownProviderError, collect_usage, format_usage_line


def _json_usage_payload(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for record in records:
        provider_record = dict(record)
        provider = provider_record.pop("provider")
        payload[provider] = provider_record
    return payload


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("provider", required=False)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON output.")
def usage_command(
    provider: str | None = None,
    json_output: bool = False,
) -> int:
    try:
        records = collect_usage(provider)
    except UnknownProviderError as exc:
        raise click.ClickException(str(exc)) from exc

    if json_output:
        click.echo(json.dumps(_json_usage_payload(records), indent=2, sort_keys=True))
        return 0

    for record in records:
        click.echo(format_usage_line(record))
    return 0


app = usage_command


def main(argv: list[str] | None = None) -> int:
    try:
        result = app.main(args=argv, prog_name="quse", standalone_mode=False)
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    if result is None:
        return 0
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
