import json
from pathlib import Path
import subprocess
import time

from quse import claude_quota, copilot_quota, zai_quota
from quse._shared import UsageStatus


def test_claude_read_access_token_from_default_shape(tmp_path: Path) -> None:
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "token-123"}}), encoding="utf-8")

    assert claude_quota._read_access_token(creds) == "token-123"


def test_claude_parse_usage_response_flags_limits() -> None:
    status = claude_quota._parse_usage_response(
        {
            "five" + "_hour": {"utilization": 81, "resets_at": "2026-04-11T00:00:00Z"},
            "seven" + "_day": {"utilization": 10, "resets_at": "2026-04-17T00:00:00Z"},
        }
    )

    assert status.limit_reached is False
    assert status.short_term.percent_remaining == 19
    assert status.long_term.percent_remaining == 90


def test_claude_check_quota_caches_fetch_result() -> None:
    claude_quota.reset_cache()
    calls: list[str] = []

    def fake_fetch(token: str):
        calls.append(token)
        return UsageStatus(checked_at=time.monotonic())

    creds = Path("/tmp/claude-creds.json")
    original = claude_quota._read_access_token
    claude_quota._read_access_token = lambda _path=None: "token-123"
    try:
        first = claude_quota.check_claude_quota(creds_path=creds, _fetch=fake_fetch)
        second = claude_quota.check_claude_quota(creds_path=creds, _fetch=fake_fetch)
    finally:
        claude_quota._read_access_token = original
        claude_quota.reset_cache()

    assert first is second
    assert calls == ["token-123"]


def test_claude_quota_block_reason_is_fail_open() -> None:
    reason = claude_quota.claude_quota_block_reason(
        _fetch=lambda _token: UsageStatus(checked_at=1.0, error="boom")
    )

    assert reason is None


def test_copilot_fetch_quota_parses_low_remaining(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "quota_reset_date": "2026-04-11T00:00:00Z",
                    "quota_snapshots": {
                        "premium_interactions": {
                            "remaining": 10,
                            "entitlement": 100,
                            "percent_remaining": 10,
                        }
                    },
                }
            ),
            stderr="",
        ),
    )

    status = copilot_quota._fetch_quota()

    assert status.limit_reached is True
    assert status.short_term.percent_remaining == 100.0
    assert status.long_term.percent_remaining == 10.0


def test_copilot_fetch_quota_handles_unlimited(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps({"quota_snapshots": {"premium_interactions": {"unlimited": True}}}),
            stderr="",
        ),
    )

    status = copilot_quota._fetch_quota()

    assert status.limit_reached is False
    assert status.long_term.percent_remaining == 100.0


def test_copilot_check_quota_caches_fetch_result() -> None:
    copilot_quota.reset_cache()
    calls = 0

    def fake_fetch():
        nonlocal calls
        calls += 1
        return UsageStatus(checked_at=time.monotonic())

    first = copilot_quota.check_copilot_quota(_fetch=fake_fetch)
    second = copilot_quota.check_copilot_quota(_fetch=fake_fetch)
    copilot_quota.reset_cache()

    assert first is second
    assert calls == 1


def test_copilot_quota_block_reason_is_fail_open() -> None:
    reason = copilot_quota.copilot_quota_block_reason(
        _fetch=lambda: UsageStatus(checked_at=1.0, error="boom")
    )

    assert reason is None


def test_zai_fetch_usage_parses_limits(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "limits": [
                        {"type": "TIME_LIMIT", "percentage": 81, "window_hours": 1, "remaining": 2, "limit": 10},
                        {"type": "TOKENS_LIMIT", "percentage": 50, "window_hours": 24, "remaining": 500, "limit": 1000},
                    ]
                }
            ),
            stderr="",
        ),
    )

    status = zai_quota._fetch_usage()

    assert status.limit_reached is False
    assert status.short_term.percent_remaining == 50.0
    assert status.long_term.percent_remaining == 100.0


def test_zai_check_quota_caches_fetch_result() -> None:
    zai_quota.reset_cache()
    calls = 0

    def fake_fetch():
        nonlocal calls
        calls += 1
        return UsageStatus(checked_at=time.monotonic())

    first = zai_quota.check_zai_quota(_fetch=fake_fetch)
    second = zai_quota.check_zai_quota(_fetch=fake_fetch)
    zai_quota.reset_cache()

    assert first is second
    assert calls == 1


def test_zai_quota_block_reason_is_fail_open() -> None:
    reason = zai_quota.zai_quota_block_reason(
        _fetch=lambda: UsageStatus(checked_at=1.0, error="boom")
    )

    assert reason is None
