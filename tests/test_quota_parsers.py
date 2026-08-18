import json
from pathlib import Path
import subprocess
import time

from quse import claude_quota, copilot_quota, grok_quota, zai_quota
from quse._shared import UsageStatus, reset_at_to_iso


def test_claude_read_access_token_from_default_shape(tmp_path: Path) -> None:
    creds = tmp_path / ".credentials.json"
    creds.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "token-123"}}), encoding="utf-8"
    )

    assert claude_quota._read_access_token(creds) == "token-123"


def test_claude_read_access_token_refreshes_expired_token(
    tmp_path: Path, monkeypatch
) -> None:
    creds = tmp_path / ".credentials.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-token",
                    "refreshToken": "refresh-token",
                    "expiresAt": 1,
                    "scopes": ["user:profile", "user:inference"],
                }
            }
        ),
        encoding="utf-8",
    )
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return json.dumps(
                {
                    "access_token": "new-token",
                    "refresh_token": "new-refresh-token",
                    "expires_in": 3600,
                    "scope": "user:profile user:inference",
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(claude_quota.urllib.request, "urlopen", fake_urlopen)

    assert claude_quota._read_access_token(creds) == "new-token"

    request, timeout = requests[0]
    assert timeout == 30.0
    assert request.full_url == "https://platform.claude.com/v1/oauth/token"
    assert request.get_header("User-agent") == "Claude-Code/2.1.198"
    body = json.loads(request.data.decode("utf-8"))
    assert body == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-token",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "scope": "user:profile user:inference",
    }
    updated = json.loads(creds.read_text(encoding="utf-8"))
    assert updated["claudeAiOauth"]["accessToken"] == "new-token"
    assert updated["claudeAiOauth"]["refreshToken"] == "new-refresh-token"
    assert updated["claudeAiOauth"]["expiresAt"] > 1


def test_claude_oauth_client_id_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_CLIENT_ID", "client-from-env")

    assert claude_quota._oauth_client_id() == "client-from-env"


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
            stdout=json.dumps(
                {"quota_snapshots": {"premium_interactions": {"unlimited": True}}}
            ),
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


def test_zai_fetch_usage_parses_limits(monkeypatch) -> None:
    status = zai_quota._parse_usage_response(
        {
            "data": {
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "percentage": 81,
                        "unit": 3,
                        "remaining": 2,
                        "usage": 10,
                    },
                    {
                        "type": "TOKENS_LIMIT",
                        "percentage": 50,
                        "unit": 6,
                        "remaining": 500,
                        "usage": 1000,
                        "nextResetTime": 1770000000000,
                    },
                    {
                        "type": "TIME_LIMIT",
                        "percentage": 1,
                        "unit": 5,
                        "remaining": 3999,
                        "usage": 4000,
                        "nextResetTime": 1772500000000,
                    },
                ]
            }
        }
    )

    assert status.limit_reached is False
    assert status.short_term.percent_remaining == 19.0
    assert status.short_term.reset_at is None
    assert status.five_hour.window_hours == 5
    assert status.long_term.percent_remaining == 50.0
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-02-02T02:40:00Z"
    assert status.weekly.window_hours is None
    assert status.monthly_web_search.percent_remaining == 99.0


def test_zai_status_accepts_legacy_window_names() -> None:
    status = zai_quota.ZaiQuotaStatus(
        api_calls=zai_quota.ZaiQuotaWindow(used_percent=25),
        tokens=zai_quota.ZaiQuotaWindow(used_percent=40),
    )

    assert status.five_hour.percent_remaining == 75.0
    assert status.weekly.percent_remaining == 60.0
    assert status.api_calls is status.five_hour
    assert status.tokens is status.weekly


def test_zai_parse_weekly_limit_reached() -> None:
    status = zai_quota._parse_usage_response(
        {
            "limits": [
                {"type": "TOKENS_LIMIT", "percentage": 20, "unit": 3},
                {
                    "type": "TOKENS_LIMIT",
                    "percentage": 100,
                    "unit": 6,
                    "nextResetTime": 1770000000000,
                },
            ]
        }
    )

    assert status.limit_reached is True
    assert status.long_term.percent_remaining == 0.0


def test_zai_reads_goz_config_shape(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "zai_token": "token-123",
                "zai_base_url": "https://api.z.ai/api/anthropic",
                "timeout": 9,
            }
        ),
        encoding="utf-8",
    )

    config = zai_quota._read_zai_config(config_path)

    assert config.token == "token-123"
    assert config.base_url == "https://api.z.ai/api/anthropic"
    assert config.timeout == 9.0


def test_grok_parse_usage_flags_weekly_and_monthly() -> None:
    status = grok_quota._parse_billing_payloads(
        monthly={
            "config": {
                "monthlyLimit": {"val": 100},
                "used": {"val": 40},
                "billingPeriodEnd": "2026-09-01T00:00:00Z",
            }
        },
        credits={
            "config": {
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
                "creditUsagePercent": 10,
                "billingPeriodEnd": "2026-08-25T00:00:00Z",
            }
        },
        user={"subscriptionTier": "SuperGrokPlus"},
    )

    assert status.limit_reached is False
    assert status.short_term.percent_remaining == 90.0
    assert status.long_term.percent_remaining == 60.0
    assert status.subscription == "SuperGrokPlus"


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
