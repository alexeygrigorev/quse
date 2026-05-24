"""Command-line interface for quse."""

from __future__ import annotations

import json

import click

from quse.usage import UnknownProviderError, collect_usage, format_usage_line


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
        if len(records) == 1:
            click.echo(json.dumps(records[0], sort_keys=True))
        else:
            for record in records:
                click.echo(json.dumps(record, sort_keys=True))
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
