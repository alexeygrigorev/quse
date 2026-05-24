import json

from click.testing import CliRunner

from quse.cli import app
from quse.codex_quota import CodexQuotaStatus, CodexQuotaWindow
from quse.usage import normalize_usage_provider


def test_usage_single_provider_json(monkeypatch):
    monkeypatch.setattr(
        "quse.check_codex_quota",
        lambda: CodexQuotaStatus(
            secondary_window=CodexQuotaWindow(
                used_percent=25,
                reset_at="2026-05-01",
            )
        ),
    )
    monkeypatch.setattr("quse.codex_quota_block_reason", lambda: None)

    result = CliRunner().invoke(app, ["codex", "--json"])

    assert result.exit_code == 0
    record = json.loads(result.stdout)
    assert record["provider"] == "codex"
    assert record["status"] == "ok"
    assert record["short_term"] == {"percent_remaining": 100.0, "reset_at": None}
    assert record["long_term"] == {"percent_remaining": 75.0, "reset_at": "2026-05-01T00:00:00Z"}


def test_usage_unknown_provider_exits_non_zero():
    result = CliRunner().invoke(app, ["wat"])

    assert result.exit_code != 0
    assert "Unknown provider 'wat'" in result.stderr


def test_zai_usage_handles_missing_limit_values(monkeypatch):
    monkeypatch.setattr("quse.check_zai_quota", lambda: __import__("quse").ZaiQuotaStatus())
    monkeypatch.setattr("quse.zai_quota_block_reason", lambda: None)

    record = normalize_usage_provider("zai")

    assert record["short_term"] == {"percent_remaining": 100.0, "reset_at": None}
    assert record["long_term"] == {"percent_remaining": 100.0, "reset_at": None}


def test_human_usage_line_uses_normalized_windows(monkeypatch):
    monkeypatch.setattr(
        "quse.check_codex_quota",
        lambda: CodexQuotaStatus(
            secondary_window=CodexQuotaWindow(
                used_percent=25,
                reset_at="2026-05-01",
            )
        ),
    )
    monkeypatch.setattr("quse.codex_quota_block_reason", lambda: None)

    result = CliRunner().invoke(app, ["codex"])

    assert result.exit_code == 0
    assert result.stdout.strip() == (
        "codex: status=ok short_term=100.0% short_reset=unknown "
        "long_term=75.0% long_reset=2026-05-01T00:00:00Z"
    )
