"""Proactive codex quota checking via chatgpt.com API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import time
from pathlib import Path

import urllib.error
import urllib.request

from quse._shared import UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_AUTH_PATH = Path.home() / ".codex" / "auth.json"
_CACHE_TTL_SECONDS = 60


def _normalize_codex_reset_at(value: object) -> str | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return normalize_reset_at(value)


@dataclass(slots=True)
class CodexQuotaWindow:
    used_percent: float = 0.0
    reset_at: str | None = None

    def __post_init__(self) -> None:
        self.used_percent = float(self.used_percent)
        self.reset_at = _normalize_codex_reset_at(self.reset_at)

    @property
    def percent_remaining(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass(slots=True)
class CodexQuotaStatus:
    primary_window: CodexQuotaWindow = field(default_factory=CodexQuotaWindow)
    secondary_window: CodexQuotaWindow = field(default_factory=CodexQuotaWindow)
    limit_reached: bool = False
    checked_at: float = 0.0
    error: str | None = None

    @property
    def short_term(self) -> UsageWindow:
        return UsageWindow(
            percent_remaining=self.primary_window.percent_remaining,
            reset_at=self.primary_window.reset_at,
        )

    @property
    def long_term(self) -> UsageWindow:
        return UsageWindow(
            percent_remaining=self.secondary_window.percent_remaining,
            reset_at=self.secondary_window.reset_at,
        )

    @property
    def earliest_reset_at(self) -> str | None:
        reset_candidates = [value for value in (self.primary_window.reset_at, self.secondary_window.reset_at) if value]
        if not reset_candidates:
            return None
        return min(reset_candidates)


_cached_status: CodexQuotaStatus | None = None


def _read_bearer_token(auth_path: Path | None = None) -> str | None:
    path = auth_path or _AUTH_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = data.get("tokens", {}).get("access_token")
        if token:
            return token
        logger.warning("codex auth.json missing tokens.access_token")
        return None
    except FileNotFoundError:
        logger.warning("codex auth.json not found at %s", path)
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("codex auth.json parse error: %s", exc)
        return None


def _parse_quota_response(data: dict) -> CodexQuotaStatus:
    rate_limit = data.get("rate_limit")
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    primary_data = rate_limit.get("primary_window")
    if not isinstance(primary_data, dict):
        primary_data = {}
    secondary_data = rate_limit.get("secondary_window")
    if not isinstance(secondary_data, dict):
        secondary_data = {}

    primary_window = CodexQuotaWindow(
        used_percent=primary_data.get("used_percent", 0),
        reset_at=primary_data.get("reset_at"),
    )
    secondary_window = CodexQuotaWindow(
        used_percent=secondary_data.get("used_percent", 0),
        reset_at=secondary_data.get("reset_at"),
    )

    return CodexQuotaStatus(
        primary_window=primary_window,
        secondary_window=secondary_window,
        limit_reached=bool(rate_limit.get("limit_reached", False)) or secondary_window.used_percent >= 80.0,
        checked_at=time.monotonic(),
    )


def _fetch_quota(token: str, *, timeout: float = 10.0) -> CodexQuotaStatus:
    req = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_quota_response(data)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        logger.warning("codex quota check failed (fail-open): %s", exc)
        return CodexQuotaStatus(checked_at=time.monotonic(), error=str(exc))


def check_codex_quota(
    *,
    auth_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> CodexQuotaStatus:
    """Check codex quota proactively. Returns cached result within TTL.

    Fails open: if auth is missing or API call fails, returns a non-blocking status.
    """
    global _cached_status
    now = time.monotonic()

    if _cached_status is not None and (now - _cached_status.checked_at) < cache_ttl:
        return _cached_status

    token = _read_bearer_token(auth_path)
    if token is None:
        status = CodexQuotaStatus(checked_at=now, error="no auth token")
        _cached_status = status
        return status

    fetcher = _fetch_quota
    if callable(_fetch):
        fetcher = _fetch
    status = fetcher(token)
    _cached_status = status
    return status


def codex_quota_block_reason(
    *,
    auth_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> str | None:
    """Return a blocking reason string if codex quota is exhausted, or None if OK."""
    status = check_codex_quota(auth_path=auth_path, cache_ttl=cache_ttl, _fetch=_fetch)
    if status.error is not None:
        return None  # fail-open
    if status.limit_reached:
        if status.long_term.reset_at:
            return f"codex quota exhausted (weekly window at {status.long_term.used_percent:.0f}%, resets {status.long_term.reset_at})"
        return f"codex quota exhausted (weekly window at {status.long_term.used_percent:.0f}%)"
    return None


def reset_cache() -> None:
    """Clear the cached quota status (useful for testing)."""
    global _cached_status
    _cached_status = None
