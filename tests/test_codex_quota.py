"""Tests for proactive codex quota checking."""

import json

import pytest

from quse._shared import reset_at_to_iso
from quse.codex_quota import (
    _fetch_quota,
    _parse_quota_response,
    _parse_reset_credits_response,
    _read_bearer_token,
    check_codex_quota,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


# --- Token reading ---


def test_read_token_success(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok-abc"}}))
    assert _read_bearer_token(auth) == "tok-abc"


def test_read_token_missing_file(tmp_path):
    assert _read_bearer_token(tmp_path / "nope.json") is None


def test_read_token_bad_json(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("not json")
    assert _read_bearer_token(auth) is None


def test_read_token_missing_field(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {}}))
    assert _read_bearer_token(auth) is None


# --- Response parsing ---


def _make_api_response(
    limit_reached=False,
    primary_pct=50.0,
    secondary_pct=20.0,
    primary_reset="2026-04-08T05:00:00Z",
    secondary_reset="2026-04-13T00:00:00Z",
):
    return {
        "rate_limit": {
            "limit_reached": limit_reached,
            "primary_window": {
                "used_percent": primary_pct,
                "reset_at": primary_reset,
            },
            "secondary_window": {
                "used_percent": secondary_pct,
                "reset_at": secondary_reset,
            },
        }
    }


def test_parse_limit_not_reached():
    status = _parse_quota_response(_make_api_response(limit_reached=False))
    assert status.limit_reached is False
    assert status.short_term.percent_remaining == 50.0
    assert reset_at_to_iso(status.short_term.reset_at) == "2026-04-08T05:00:00Z"
    assert status.long_term.percent_remaining == 80.0
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-04-13T00:00:00Z"


def test_parse_limit_reached():
    status = _parse_quota_response(
        _make_api_response(limit_reached=True, secondary_pct=85.0)
    )
    assert status.limit_reached is True


def test_parse_empty_response():
    status = _parse_quota_response({})
    assert status.limit_reached is False
    assert status.short_term.percent_remaining == 100.0
    assert status.long_term.percent_remaining == 100.0


def test_parse_unix_reset_timestamps():
    status = _parse_quota_response(
        _make_api_response(
            primary_reset=1779637981,
            secondary_reset=1780173234,
        )
    )

    assert reset_at_to_iso(status.short_term.reset_at) == "2026-05-24T15:53:01Z"
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-05-30T20:33:54Z"


def test_parse_millisecond_reset_timestamps():
    status = _parse_quota_response(
        _make_api_response(
            primary_reset=1779637981000,
            secondary_reset=1780173234000,
        )
    )

    assert reset_at_to_iso(status.short_term.reset_at) == "2026-05-24T15:53:01Z"
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-05-30T20:33:54Z"


def test_parse_reset_credits_response():
    credits = _parse_reset_credits_response(
        {
            "credits": [
                {
                    "status": "available",
                    "title": "Usage reset",
                    "expires_at": "2026-05-24T15:53:01Z",
                },
                {
                    "status": "expired",
                    "title": "Old reset",
                    "expires_at": 1779637981,
                },
                "ignored",
            ]
        }
    )

    assert len(credits) == 2
    assert credits[0].is_available is True
    assert credits[0].title == "Usage reset"
    assert reset_at_to_iso(credits[0].expires_at) == "2026-05-24T15:53:01Z"
    assert credits[1].is_available is False
    assert reset_at_to_iso(credits[1].expires_at) == "2026-05-24T15:53:01Z"


def test_parse_reset_credits_response_handles_missing_list():
    assert _parse_reset_credits_response({}) == []


# --- Quota check with caching ---


def test_check_quota_limit_reached(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    def fake_fetch(token):
        return _parse_quota_response(
            _make_api_response(limit_reached=True, secondary_pct=85.0)
        )

    status = check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    assert status.limit_reached is True


def test_check_quota_not_reached(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    def fake_fetch(token):
        return _parse_quota_response(
            _make_api_response(limit_reached=False, secondary_pct=30.0)
        )

    status = check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    assert status.limit_reached is False


def test_check_quota_cache(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    call_count = 0

    def fake_fetch(token):
        nonlocal call_count
        call_count += 1
        return _parse_quota_response(_make_api_response())

    check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    assert call_count == 1  # second call used cache


def test_check_quota_cache_expires(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    call_count = 0

    def fake_fetch(token):
        nonlocal call_count
        call_count += 1
        return _parse_quota_response(_make_api_response())

    check_codex_quota(auth_path=auth, cache_ttl=0, _fetch=fake_fetch)
    check_codex_quota(auth_path=auth, cache_ttl=0, _fetch=fake_fetch)
    assert call_count == 2  # cache expired immediately


def test_fetch_quota_fetches_reset_credits(monkeypatch):
    requested_urls = []

    def fake_fetch_json(url, token, *, timeout):
        requested_urls.append(url)
        assert token == "tok"
        assert timeout == 3.0
        if url.endswith("/usage"):
            return _make_api_response()
        return {
            "credits": [
                {
                    "status": "available",
                    "title": "Usage reset",
                    "expires_at": "2026-05-24T15:53:01Z",
                }
            ]
        }

    monkeypatch.setattr("quse.codex_quota._fetch_json", fake_fetch_json)

    status = _fetch_quota("tok", timeout=3.0)

    assert [url.rsplit("/", 1)[-1] for url in requested_urls] == [
        "usage",
        "rate-limit-reset-credits",
    ]
    assert status.error is None
    assert status.reset_credits_error is None
    assert len(status.reset_credits) == 1
    assert len(status.available_reset_credits) == 1


def test_fetch_quota_reset_credits_failure_is_non_blocking(monkeypatch):
    def fake_fetch_json(url, token, *, timeout):
        if url.endswith("/usage"):
            return _make_api_response()
        raise OSError("credits unavailable")

    monkeypatch.setattr("quse.codex_quota._fetch_json", fake_fetch_json)

    status = _fetch_quota("tok")

    assert status.error is None
    assert status.limit_reached is False
    assert status.reset_credits == []
    assert status.reset_credits_error == "credits unavailable"


def test_check_quota_no_auth(tmp_path):
    status = check_codex_quota(auth_path=tmp_path / "nope.json")
    assert status.error == "no auth token"
    assert status.limit_reached is False


# Monitoring integration tests live in the litehive test suite — they exercise
# litehive.observability.record_codex_quota_check against heru's UsageStatus,
# which is a litehive-side concern.
