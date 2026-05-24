import json
import os
import time

from click.testing import CliRunner

from quse.cli import app
from quse.codex_quota import CodexQuotaStatus, CodexQuotaWindow
from quse.usage import normalize_usage_provider


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
    assert record["provider"] == "codex"
    assert record["status"] == "ok"
    assert record["short_term"] == {"percent_remaining": 60.0, "reset_at": "2026-04-30T00:00:00Z"}
    assert record["long_term"] == {"percent_remaining": 75.0, "reset_at": "2026-05-01T00:00:00Z"}


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
