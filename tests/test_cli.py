import json
import os
import time

from click.testing import CliRunner

from quse.cli import app
from quse.codex_quota import CodexQuotaStatus, CodexQuotaWindow
from quse.usage import collect_usage, format_usage_line, normalize_usage_provider
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
            )
        ),
    )
    monkeypatch.setattr("quse.usage.codex_quota_block_reason", lambda: None)

    result = CliRunner().invoke(app, ["codex", "--json"])

    assert result.exit_code == 0
    record = json.loads(result.stdout)
    assert set(record) == {"codex"}
    assert record["codex"]["status"] == "ok"
    assert record["codex"]["short_term"] == {"percent_remaining": 60.0, "reset_at": "2026-04-30T00:00:00Z"}
    assert record["codex"]["long_term"] == {"percent_remaining": 75.0, "reset_at": "2026-05-01T00:00:00Z"}
    assert result.stdout.startswith("{\n  ")


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


def test_usage_unknown_provider_exits_non_zero():
    result = CliRunner().invoke(app, ["wat"])

    assert result.exit_code != 0
    assert "Unknown provider 'wat'" in result.stderr


def test_zai_usage_handles_missing_limit_values(monkeypatch):
    monkeypatch.setattr("quse.usage.check_zai_quota", lambda: __import__("quse").ZaiQuotaStatus())
    monkeypatch.setattr("quse.usage.zai_quota_block_reason", lambda: None)

    record = normalize_usage_provider("zai")

    assert record["short_term"] == {"percent_remaining": 100.0, "reset_at": None}
    assert record["long_term"] == {"percent_remaining": 100.0, "reset_at": None}


def test_zai_human_usage_shows_rolling_windows(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_zai_quota",
        lambda: ZaiQuotaStatus(
            api_calls=ZaiQuotaWindow(used_percent=0, window_hours=5),
            tokens=ZaiQuotaWindow(used_percent=0, window_hours=3),
        ),
    )
    monkeypatch.setattr("quse.usage.zai_quota_block_reason", lambda: None)

    record = normalize_usage_provider("zai")

    assert format_usage_line(record) == (
        "zai:\n"
        "    status: ok\n"
        "    short_term:\n"
        "        usage: 100.0%\n"
        "        reset: rolling 5h\n"
        "    long_term:\n"
        "        usage: 100.0%\n"
        "        reset: rolling 3h"
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
    assert records == [{"provider": "codex"}, {"provider": "claude"}, {"provider": "copilot"}, {"provider": "zai"}]
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
            )
        ),
    )
    monkeypatch.setattr("quse.usage.codex_quota_block_reason", lambda: None)

    try:
        result = CliRunner().invoke(app, ["codex"])

        assert result.exit_code == 0
        assert result.stdout.strip() == (
            "codex:\n"
            "    status: ok\n"
            "    short_term:\n"
            "        usage: 60.0%\n"
            "        reset: 30-04-2026 00:00 (UTC)\n"
            "    long_term:\n"
            "        usage: 75.0%\n"
            "        reset: 01-05-2026 00:00 (UTC)"
        )
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        if hasattr(time, "tzset"):
            time.tzset()
