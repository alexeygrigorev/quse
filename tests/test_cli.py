import json
import os
import time

from click.testing import CliRunner

from quse._shared import UsageWindow
from quse.claude_quota import ClaudeQuotaStatus
from quse.cli import app
from quse.codex_quota import CodexQuotaStatus, CodexQuotaWindow, CodexResetCredit
from quse.copilot_quota import CopilotQuotaStatus
from quse.usage import (
    collect_usage,
    format_usage_line,
    normalize_usage_provider,
    usage_window_record,
)
from quse.zai_quota import ZaiQuotaStatus, ZaiQuotaWindow


def test_usage_single_provider_json(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_codex_quota",
        lambda: CodexQuotaStatus(
            primary_window=CodexQuotaWindow(
                used_percent=40,
                reset_at="2026-04-30",
            ),
            secondary_window=CodexQuotaWindow(
                used_percent=25,
                reset_at="2026-05-01",
            ),
        ),
    )

    result = CliRunner().invoke(app, ["codex", "--json"])

    assert result.exit_code == 0
    record = json.loads(result.stdout)
    assert set(record) == {"codex"}
    assert record["codex"]["status"] == "ok"
    assert record["codex"]["short_term"] == {
        "percent_remaining": 60.0,
        "reset_at": "2026-04-30T00:00:00Z",
        "window": "5h",
    }
    assert record["codex"]["long_term"] == {
        "percent_remaining": 75.0,
        "reset_at": "2026-05-01T00:00:00Z",
        "window": "7d",
    }
    assert result.stdout.startswith("{\n  ")


def test_codex_json_includes_reset_credits(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_codex_quota",
        lambda: CodexQuotaStatus(
            reset_credits=[
                CodexResetCredit(
                    status="available",
                    title="Full reset (Weekly + 5 hr)",
                    expires_at="2026-05-24T15:53:01Z",
                )
            ],
        ),
    )

    result = CliRunner().invoke(app, ["codex", "--json"])

    assert result.exit_code == 0
    details = json.loads(result.stdout)["codex"]["details"]
    assert details["reset_credits_available"] == 1
    assert details["reset_credits"] == [
        {
            "status": "available",
            "title": "Full reset (Weekly + 5 hr)",
            "expires_at": "2026-05-24T15:53:01Z",
        }
    ]


def test_codex_human_usage_shows_reset_credits(monkeypatch):
    original_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setattr(
        "quse.usage.check_codex_quota",
        lambda: CodexQuotaStatus(
            reset_credits=[
                CodexResetCredit(
                    status="available",
                    title="Full reset (Weekly + 5 hr)",
                    expires_at="2026-05-24T15:53:01Z",
                )
            ],
        ),
    )

    try:
        result = CliRunner().invoke(app, ["codex"])

        assert result.exit_code == 0
        assert (
            "reset_credits:\n    expires: 24-05-2026 15:53 (UTC) / overdue"
        ) in result.stdout
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        if hasattr(time, "tzset"):
            time.tzset()


def test_codex_human_usage_omits_absent_short_term_window(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_codex_quota",
        lambda: CodexQuotaStatus(
            primary_window=CodexQuotaWindow(
                used_percent=90,
                limit_window_seconds=604800,
            ),
            secondary_window=CodexQuotaWindow(present=False),
        ),
    )

    result = CliRunner().invoke(app, ["codex"])

    assert result.exit_code == 0
    assert result.stdout == (
        "long_term:\n"
        "    remaining: 10.0%\n"
        "    reset: unknown\n"
    )


def test_usage_all_providers_json_is_keyed_by_provider(monkeypatch):
    records = [
        {"provider": "codex", "status": "ok", "error": None},
        {"provider": "claude", "status": "ok", "error": None},
    ]
    monkeypatch.setattr("quse.cli.collect_usage", lambda provider: records)

    result = CliRunner().invoke(app, ["--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "claude": {"error": None, "status": "ok"},
        "codex": {"error": None, "status": "ok"},
    }
    assert result.stdout.startswith("{\n  ")


def test_usage_window_record_unified_schema():
    """Every provider record has the SAME unified shape; each window carries a
    span label. This is the single schema downstream reads — no provider-specific
    branching required of consumers."""
    record = usage_window_record(
        provider="claude",
        status="ok",
        short_term=UsageWindow(percent_remaining=90.0, window="5h"),
        long_term=UsageWindow(percent_remaining=80.0, window="7d"),
    )
    assert set(record) == {
        "provider",
        "status",
        "short_term",
        "long_term",
        "error",
        "details",
    }
    assert set(record["short_term"]) == {"percent_remaining", "reset_at", "window"}
    assert set(record["long_term"]) == {"percent_remaining", "reset_at", "window"}
    assert record["short_term"]["window"] == "5h"
    assert record["long_term"]["window"] == "7d"


def test_provider_window_span_labels_are_authoritative():
    """quse owns the concrete span label for every provider window — downstream
    consumers must not re-derive it."""
    for status, expected in (
        (ClaudeQuotaStatus(), ("5h", "7d")),
        (CodexQuotaStatus(), ("5h", "7d")),
        (CopilotQuotaStatus(), (None, "monthly")),
        (ZaiQuotaStatus(), ("5h", "weekly")),
    ):
        assert (status.short_term.window, status.long_term.window) == expected


def test_usage_unknown_provider_exits_non_zero():
    result = CliRunner().invoke(app, ["wat"])

    assert result.exit_code != 0
    assert "Unknown provider 'wat'" in result.stderr


def test_zai_usage_handles_missing_limit_values(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_zai_quota", lambda: __import__("quse").ZaiQuotaStatus()
    )

    record = normalize_usage_provider("zai")

    assert record["short_term"] == {
        "percent_remaining": 100.0,
        "reset_at": None,
        "window": "5h",
    }
    assert record["long_term"] == {
        "percent_remaining": 100.0,
        "reset_at": None,
        "window": "weekly",
    }


def test_zai_human_usage_shows_rolling_windows(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_zai_quota",
        lambda: ZaiQuotaStatus(
            five_hour=ZaiQuotaWindow(used_percent=0, window_hours=5),
            weekly=ZaiQuotaWindow(used_percent=0),
        ),
    )

    record = normalize_usage_provider("zai")

    assert format_usage_line(record) == (
        "zai:\n"
        "    short_term:\n"
        "        remaining: 100.0%\n"
        "        reset: rolling 5h\n"
        "    long_term:\n"
        "        remaining: 100.0%\n"
        "        reset: weekly"
    )


def test_collect_usage_without_provider_runs_checks_in_parallel(monkeypatch):
    calls: list[str] = []

    def fake_normalize(provider: str) -> dict:
        calls.append(provider)
        time.sleep(0.1)
        return {"provider": provider}

    monkeypatch.setattr("quse.usage.normalize_usage_provider", fake_normalize)

    started_at = time.monotonic()
    records = collect_usage()
    elapsed = time.monotonic() - started_at

    assert sorted(calls) == ["claude", "codex", "copilot", "zai"]
    assert records == [
        {"provider": "codex"},
        {"provider": "claude"},
        {"provider": "zai"},
        {"provider": "copilot"},
    ]
    assert elapsed < 0.25


def test_human_usage_line_uses_normalized_windows(monkeypatch):
    original_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setattr(
        "quse.usage.check_codex_quota",
        lambda: CodexQuotaStatus(
            primary_window=CodexQuotaWindow(
                used_percent=40,
                reset_at="2026-04-30",
            ),
            secondary_window=CodexQuotaWindow(
                used_percent=25,
                reset_at="2026-05-01",
            ),
        ),
    )

    try:
        result = CliRunner().invoke(app, ["codex"])

        assert result.exit_code == 0
        assert result.stdout.strip() == (
            "short_term:\n"
            "    remaining: 60.0%\n"
            "    reset: 30-04-2026 00:00 (UTC) / overdue\n"
            "long_term:\n"
            "    remaining: 75.0%\n"
            "    reset: 01-05-2026 00:00 (UTC) / overdue"
        )
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        if hasattr(time, "tzset"):
            time.tzset()
