"""Tests for Grok Build quota checking."""

import json
from pathlib import Path
import time

from quse import grok_quota
from quse._shared import UsageStatus, reset_at_to_iso


def _encode_varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _encode_bytes_field(field: int, value: bytes) -> bytes:
    tag = _encode_varint((field << 3) | 2)
    return tag + _encode_varint(len(value)) + value


def _encode_varint_field(field: int, value: int) -> bytes:
    return _encode_varint(field << 3) + _encode_varint(value)


def _grpc_frame(payload: bytes) -> bytes:
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def _grpc_trailer(payload: bytes) -> bytes:
    return b"\x80" + len(payload).to_bytes(4, "big") + payload


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


def test_reset_cookie_from_auth_reads_only_web_cookie_fields(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "https://auth.x.ai::client": {
                    "key": "cli-token-must-not-be-sent-as-a-cookie",
                    "refresh_token": "refresh-token-must-not-be-sent",
                    "cookies": {
                        "sso": "browser-session",
                        "sso-rw": "browser-session-write",
                        "cf_clearance": "clearance",
                    },
                    "user_agent": "browser-agent",
                }
            }
        ),
        encoding="utf-8",
    )

    cookie = grok_quota._reset_cookie_from_auth(auth)

    assert cookie == (
        "sso=browser-session; sso-rw=browser-session-write; "
        "cf_clearance=clearance"
    )
    assert "cli-token" not in cookie
    assert "refresh-token" not in cookie
    assert grok_quota._reset_user_agent_from_auth(auth) == "browser-agent"


def test_reset_cookie_from_file_accepts_cookie_editor_json(
    tmp_path: Path, monkeypatch
) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(
        json.dumps(
            [
                {"name": "sso", "value": "browser-session"},
                {"name": "sso-rw", "value": "browser-session-write"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GROK_RESET_COOKIE_FILE", str(cookie_file))

    assert grok_quota._reset_cookie_from_env() == (
        "sso=browser-session; sso-rw=browser-session-write"
    )


def test_reset_cookie_from_file_accepts_curl_export(
    tmp_path: Path, monkeypatch
) -> None:
    request_file = tmp_path / "reset.curl"
    request_file.write_text(
        "curl 'https://grok.com/prod_mc_billing.ConsumerUiSvc/GetRemainingResets' "
        "-H 'content-type: application/grpc-web+proto' "
        "-H 'cookie: sso=browser-session; sso-rw=browser-session-write; "
        "cf_clearance=clearance' "
        "-H 'user-agent: Browser/1.0'",
        encoding="utf-8",
    )
    monkeypatch.setenv("GROK_RESET_CURL_FILE", str(request_file))

    assert grok_quota._reset_cookie_from_file() == (
        "sso=browser-session; sso-rw=browser-session-write; "
        "cf_clearance=clearance"
    )
    assert grok_quota._reset_user_agent_from_file() == "Browser/1.0"


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


def test_parse_remaining_resets_json_response() -> None:
    resets = grok_quota._parse_resets_response(
        {
            "tokens": [
                {
                    "tokenId": "reset-1",
                    "validityEnd": "2026-09-01T00:00:00Z",
                },
                {
                    "token_id": "reset-2",
                    "validity_end": {"seconds": 1798848000},
                },
                {"tokenId": "reset-1", "validityEnd": "2026-09-01T00:00:00Z"},
                "ignored",
            ]
        }
    )

    assert len(resets) == 2
    assert [reset.token_id for reset in resets] == ["reset-1", "reset-2"]
    assert reset_at_to_iso(resets[0].expires_at) == "2026-09-01T00:00:00Z"
    assert reset_at_to_iso(resets[1].expires_at) == "2027-01-02T00:00:00Z"
    assert all(reset.is_available for reset in resets)


def test_parse_remaining_resets_grpc_web_response() -> None:
    first_timestamp = _encode_varint_field(1, 1_793_884_800)
    first_token = _encode_bytes_field(
        1,
        _encode_bytes_field(1, b"reset-1")
        + _encode_bytes_field(3, first_timestamp),
    )
    second_timestamp = _encode_varint_field(1, 1_800_000_000)
    second_token = _encode_bytes_field(
        1,
        _encode_bytes_field(1, b"reset-2")
        + _encode_bytes_field(3, second_timestamp),
    )

    resets = grok_quota._parse_resets_response(
        _grpc_frame(first_token + second_token)
    )

    assert [reset.token_id for reset in resets] == ["reset-1", "reset-2"]
    assert reset_at_to_iso(resets[0].expires_at) == "2026-11-05T13:20:00Z"
    assert reset_at_to_iso(resets[1].expires_at) == "2027-01-15T08:00:00Z"


def test_parse_grpc_web_error_trailer() -> None:
    trailer = _grpc_trailer(
        b"grpc-status: 7\r\ngrpc-message: Permission%20denied\r\n"
    )

    assert grok_quota._grpc_status_error(trailer) == "Permission denied"
    assert (
        grok_quota._grpc_status_error(
            _grpc_trailer(b"grpc-status: 0\r\n")
        )
        is None
    )


def test_parse_remaining_resets_marks_expired_reset_unavailable() -> None:
    resets = grok_quota._parse_resets_response(
        {"stillRedeemable": [{"tokenId": "old", "validityEnd": "2020-01-01"}]}
    )

    assert len(resets) == 1
    assert resets[0].is_available is False


def test_fetch_quota_fetches_remaining_resets(monkeypatch) -> None:
    requested_urls: list[str] = []
    monkeypatch.setenv("GROK_RESET_COOKIE", "sso=browser-session; cf_clearance=clearance")
    monkeypatch.setenv("GROK_RESET_USER_AGENT", "browser-agent")

    def fake_fetch_json(url, token, *, timeout):
        requested_urls.append(url)
        assert token == "tok"
        assert timeout == 3.0
        return {}

    def fake_fetch_grpc(url, token, *, timeout, cookie, user_agent):
        requested_urls.append(url)
        assert token == "tok"
        assert timeout == 3.0
        assert cookie == "sso=browser-session; cf_clearance=clearance"
        assert user_agent == "browser-agent"
        return json.dumps(
            {
                "tokens": [
                    {
                        "tokenId": "reset-1",
                        "validityEnd": "2026-09-01T00:00:00Z",
                    }
                ]
            }
        ).encode("utf-8")

    monkeypatch.setattr(grok_quota, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(grok_quota, "_fetch_grpc", fake_fetch_grpc)

    status = grok_quota._fetch_quota(
        "tok", base_url="https://example.test/v1", timeout=3.0
    )

    assert requested_urls == [
        "https://example.test/v1/billing",
        "https://example.test/v1/billing?format=credits",
        "https://example.test/v1/user?include=subscription",
        "https://example.test/v1/prod_mc_billing.ConsumerUiSvc/GetRemainingResets",
    ]
    assert status.resets_error is None
    assert len(status.resets) == 1
    assert len(status.available_resets) == 1


def test_fetch_resets_tries_second_rpc_after_empty_response(monkeypatch) -> None:
    requested_urls: list[str] = []

    def fake_fetch_grpc(url, token, *, timeout, cookie, user_agent):
        requested_urls.append(url)
        if url.endswith("prod_mc_billing.ConsumerUiSvc/GetRemainingResets"):
            return b""
        return json.dumps(
            {
                "tokens": [
                    {
                        "tokenId": "reset-2",
                        "validityEnd": "2026-09-01T00:00:00Z",
                    }
                ]
            }
        ).encode("utf-8")

    monkeypatch.setattr(grok_quota, "_fetch_grpc", fake_fetch_grpc)

    resets = grok_quota._fetch_resets(
        "tok", base_url="https://example.test", cookie="sso=browser-session"
    )

    assert [reset.token_id for reset in resets] == ["reset-2"]
    assert requested_urls == [
        "https://example.test/prod_mc_billing.ConsumerUiSvc/GetRemainingResets",
        "https://example.test/grok_api_v2.ConsumerUiSvc/GetRemainingResets",
    ]


def test_fetch_quota_reset_list_failure_is_non_blocking(monkeypatch) -> None:
    monkeypatch.setenv("GROK_RESET_COOKIE", "sso=browser-session")

    def fake_fetch_json(url, token, *, timeout):
        return {}

    def fake_fetch_grpc(url, token, *, timeout, cookie, user_agent):
        raise OSError("resets unavailable")

    monkeypatch.setattr(grok_quota, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(grok_quota, "_fetch_grpc", fake_fetch_grpc)

    status = grok_quota._fetch_quota("tok", base_url="https://example.test/v1")

    assert status.error is None
    assert status.resets == []
    assert status.resets_error == "resets unavailable"


def test_fetch_grpc_uses_browser_session_headers(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return b"response"

    def fake_urlopen(request, timeout):
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        captured["data"] = request.data
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(grok_quota.urllib.request, "urlopen", fake_urlopen)

    assert (
        grok_quota._fetch_grpc(
            "https://grok.example.test/prod_mc_billing.ConsumerUiSvc/GetRemainingResets",
            "cli-token",
            timeout=4.0,
            cookie="sso=browser-session; cf_clearance=clearance",
            user_agent="browser-agent",
        )
        == b"response"
    )
    assert captured["data"] == b"\x00\x00\x00\x00\x00"
    assert captured["timeout"] == 4.0
    assert captured["headers"]["cookie"] == "sso=browser-session; cf_clearance=clearance"
    assert captured["headers"]["origin"] == "https://grok.example.test"
    assert captured["headers"]["referer"] == "https://grok.example.test/"
    assert captured["headers"]["user-agent"] == "browser-agent"
    assert "authorization" not in captured["headers"]


def test_fetch_grpc_uses_curl_for_grok_rpc(monkeypatch) -> None:
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = b"response\n200"
        stderr = b""

    def fake_run(args, *, input, capture_output, timeout, check):
        captured["args"] = args
        captured["input"] = input
        captured["capture_output"] = capture_output
        captured["timeout"] = timeout
        captured["check"] = check
        return FakeResult()

    monkeypatch.setattr(grok_quota.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(grok_quota.subprocess, "run", fake_run)

    assert (
        grok_quota._fetch_grpc(
            "https://grok.com/prod_mc_billing.ConsumerUiSvc/GetRemainingResets",
            "cli-token",
            timeout=4.0,
        )
        == b"response"
    )
    assert captured["input"] == b"\x00\x00\x00\x00\x00"
    assert captured["timeout"] == 4.0
    assert captured["capture_output"] is True
    assert captured["check"] is False
    assert "Authorization: Bearer cli-token" in captured["args"]


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


def test_check_quota_passes_auth_web_session_to_fetcher(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "entry": {
                    "key": "cli-token",
                    "cookies": {
                        "sso": "browser-session",
                        "sso-rw": "browser-session-write",
                    },
                    "user_agent": "browser-agent",
                }
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_fetch(token: str, **kwargs):
        captured["token"] = token
        captured.update(kwargs)
        return UsageStatus(checked_at=time.monotonic())

    original_read = grok_quota._read_access_token
    original_fetch = grok_quota._fetch_quota
    grok_quota._read_access_token = lambda _path=None: "cli-token"
    grok_quota._fetch_quota = fake_fetch
    grok_quota.reset_cache()
    try:
        grok_quota.check_grok_quota(auth_path=auth, cache_ttl=0)
    finally:
        grok_quota._read_access_token = original_read
        grok_quota._fetch_quota = original_fetch
        grok_quota.reset_cache()

    assert captured == {
        "token": "cli-token",
        "reset_cookie": "sso=browser-session; sso-rw=browser-session-write",
        "reset_user_agent": "browser-agent",
    }


def test_check_quota_without_credentials() -> None:
    grok_quota.reset_cache()
    try:
        status = grok_quota.check_grok_quota(auth_path=Path("/tmp/missing-grok-auth.json"))
    finally:
        grok_quota.reset_cache()

    assert status.error == "no-credentials"
