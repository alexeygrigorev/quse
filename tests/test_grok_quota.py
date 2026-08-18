"""Tests for Grok Build quota checking."""

import json
from pathlib import Path
import time

from quse import grok_quota
from quse._shared import UsageStatus, reset_at_to_iso


def test_read_access_token_from_auth_json(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "https://auth.x.ai::client": {
                    "key": "token-123",
                    "oidc_issuer": "https://auth.x.ai",
                    "oidc_client_id": "client",
                }
            }
        ),
        encoding="utf-8",
    )

    assert grok_quota._read_access_token(auth) == "token-123"


def test_read_access_token_refreshes_expired_token(tmp_path: Path, monkeypatch) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "https://auth.x.ai::client": {
                    "key": "old-token",
                    "refresh_token": "refresh-token",
                    "expires_at": "2020-01-01T00:00:00Z",
                    "oidc_issuer": "https://auth.x.ai",
                    "oidc_client_id": "client-id",
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
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(grok_quota.urllib.request, "urlopen", fake_urlopen)

    assert grok_quota._read_access_token(auth) == "new-token"

    request, timeout = requests[0]
    assert timeout == 30.0
    assert request.full_url == "https://auth.x.ai/oauth2/token"
    body = request.data.decode("utf-8")
    assert "grant_type=refresh_token" in body
    assert "refresh_token=refresh-token" in body
    assert "client_id=client-id" in body
    updated = json.loads(auth.read_text(encoding="utf-8"))
    entry = updated["https://auth.x.ai::client"]
    assert entry["key"] == "new-token"
    assert entry["refresh_token"] == "new-refresh-token"
    assert entry["expires_at"].endswith("Z")


def test_parse_weekly_credits_without_numeric_quota() -> None:
    status = grok_quota._parse_billing_payloads(
        monthly={
            "config": {
                "monthlyLimit": {"val": 0},
                "used": {"val": 0},
                "billingPeriodEnd": "2026-09-01T00:00:00+00:00",
            }
        },
        credits={
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-08-18T00:08:17.671111+00:00",
                    "end": "2026-08-25T00:08:17.671111+00:00",
                },
                "onDemandCap": {"val": 0},
                "onDemandUsed": {"val": 0},
                "billingPeriodEnd": "2026-08-25T00:08:17.671111+00:00",
            }
        },
        user={"subscriptionTier": "SuperGrokPlus", "hasGrokCodeAccess": True},
    )

    assert status.short_term is None
    assert status.long_term is not None
    assert status.long_term.window == "weekly"
    assert status.long_term.percent_remaining is None
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-08-25T00:08:17Z"
    assert status.subscription == "SuperGrokPlus"
    assert status.has_grok_code_access is True
    assert status.limit_reached is False


def test_parse_credit_usage_percent_and_monthly_limit() -> None:
    status = grok_quota._parse_billing_payloads(
        monthly={
            "config": {
                "monthlyLimit": {"val": 200},
                "used": {"val": 50},
                "billingPeriodEnd": "2026-09-01T00:00:00Z",
            }
        },
        credits={
            "config": {
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
                "creditUsagePercent": 25,
                "billingPeriodEnd": "2026-08-25T00:00:00Z",
            }
        },
    )

    assert status.short_term is not None
    assert status.long_term is not None
    assert status.short_term.window == "weekly"
    assert status.short_term.percent_remaining == 75.0
    assert status.long_term.window == "monthly"
    assert status.long_term.percent_remaining == 75.0
    assert reset_at_to_iso(status.short_term.reset_at) == "2026-08-25T00:00:00Z"
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-09-01T00:00:00Z"
    assert status.limit_reached is False


def test_parse_prefers_grok_build_product_usage() -> None:
    status = grok_quota._parse_billing_payloads(
        monthly={"config": {"monthlyLimit": {"val": 0}}},
        credits={
            "config": {
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
                "creditUsagePercent": 40,
                "productUsage": [
                    {"product": "Other", "usagePercent": 90},
                    {"product": "GrokBuild", "usagePercent": 12.5},
                ],
                "billingPeriodEnd": "2026-08-25T00:00:00Z",
            }
        },
    )

    assert status.long_term is not None
    assert status.long_term.percent_remaining == 87.5
    assert status.product_usage == [
        {"product": "Other", "usage_percent": 90.0},
        {"product": "GrokBuild", "usage_percent": 12.5},
    ]


def test_parse_on_demand_cap_as_weekly_percent() -> None:
    status = grok_quota._parse_billing_payloads(
        monthly={"config": {"monthlyLimit": {"val": 0}}},
        credits={
            "config": {
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
                "onDemandCap": {"val": 80},
                "onDemandUsed": {"val": 20},
                "billingPeriodEnd": "2026-08-25T00:00:00Z",
            }
        },
    )

    assert status.long_term is not None
    assert status.long_term.percent_remaining == 75.0
    assert status.on_demand_cap == 80.0
    assert status.on_demand_used == 20.0


def test_parse_weekly_limit_reached() -> None:
    status = grok_quota._parse_billing_payloads(
        monthly={"config": {}},
        credits={
            "config": {
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
                "creditUsagePercent": 100,
                "billingPeriodEnd": "2026-08-25T00:00:00Z",
            }
        },
    )

    assert status.limit_reached is True
    assert status.long_term is not None
    assert status.long_term.percent_remaining == 0.0


def test_check_quota_caches_fetch_result() -> None:
    grok_quota.reset_cache()
    calls: list[str] = []

    def fake_fetch(token: str):
        calls.append(token)
        return UsageStatus(checked_at=time.monotonic())

    original = grok_quota._read_access_token
    grok_quota._read_access_token = lambda _path=None: "token-123"
    try:
        first = grok_quota.check_grok_quota(_fetch=fake_fetch)
        second = grok_quota.check_grok_quota(_fetch=fake_fetch)
    finally:
        grok_quota._read_access_token = original
        grok_quota.reset_cache()

    assert first is second
    assert calls == ["token-123"]


def test_check_quota_without_credentials() -> None:
    grok_quota.reset_cache()
    try:
        status = grok_quota.check_grok_quota(auth_path=Path("/tmp/missing-grok-auth.json"))
    finally:
        grok_quota.reset_cache()

    assert status.error == "no-credentials"
