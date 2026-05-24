"""Proactive Claude quota checking via OAuth usage endpoint."""

from dataclasses import dataclass, field
import json
import logging
import os
import time
from pathlib import Path

import urllib.error
import urllib.request

from quse._shared import UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CACHE_TTL_SECONDS = 60


@dataclass(slots=True)
class ClaudeQuotaWindow:
    used_percent: float = 0.0
    reset_at: str | None = None

    def __post_init__(self) -> None:
        self.used_percent = float(self.used_percent)
        self.reset_at = normalize_reset_at(self.reset_at)

    @property
    def percent_remaining(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass(slots=True)
class ClaudeQuotaStatus:
    five_hour: ClaudeQuotaWindow = field(default_factory=ClaudeQuotaWindow)
    seven_day: ClaudeQuotaWindow = field(default_factory=ClaudeQuotaWindow)
    limit_reached: bool = False
    checked_at: float = 0.0
    error: str | None = None
    subscription: str | None = None

    @property
    def short_term(self) -> UsageWindow:
        return UsageWindow(percent_remaining=self.five_hour.percent_remaining, reset_at=self.five_hour.reset_at)

    @property
    def long_term(self) -> UsageWindow:
        return UsageWindow(percent_remaining=self.seven_day.percent_remaining, reset_at=self.seven_day.reset_at)


def _default_credentials_path() -> Path:
    """Resolve Claude credentials path, respecting config dir overrides."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


_cached_status: ClaudeQuotaStatus | None = None


def _read_access_token(creds_path: Path | None = None) -> str | None:
    path = creds_path or _default_credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        if token:
            return token
        logger.warning("claude credentials missing claudeAiOauth.accessToken")
        return None
    except FileNotFoundError:
        logger.warning("claude credentials not found at %s", path)
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("claude credentials parse error: %s", exc)
        return None


def _parse_usage_response(data: dict) -> ClaudeQuotaStatus:
    five_hour_data = data.get("five_hour")
    if not isinstance(five_hour_data, dict):
        five_hour_data = {}
    seven_day_data = data.get("seven_day")
    if not isinstance(seven_day_data, dict):
        seven_day_data = {}

    five_hour = ClaudeQuotaWindow(
        used_percent=five_hour_data.get("utilization", 0),
        reset_at=five_hour_data.get("resets_at"),
    )
    seven_day = ClaudeQuotaWindow(
        used_percent=seven_day_data.get("utilization", 0),
        reset_at=seven_day_data.get("resets_at"),
    )
    subscription = data.get("subscription")

    return ClaudeQuotaStatus(
        five_hour=five_hour,
        seven_day=seven_day,
        limit_reached=seven_day.percent_remaining <= 5.0,
        checked_at=time.monotonic(),
        subscription=subscription if isinstance(subscription, str) and subscription else None,
    )


def _fetch_usage(token: str, *, timeout: float = 10.0) -> ClaudeQuotaStatus:
    req = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_usage_response(data)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        logger.warning("claude quota check failed (fail-open): %s", exc)
        return ClaudeQuotaStatus(checked_at=time.monotonic(), error=str(exc))


def check_claude_quota(
    *,
    creds_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> ClaudeQuotaStatus:
    """Check Claude quota proactively. Returns cached result within TTL.

    Fails open: if auth is missing or API call fails, returns a non-blocking status.
    """
    global _cached_status
    if _cached_status is not None and time.monotonic() - _cached_status.checked_at < cache_ttl:
        return _cached_status

    token = _read_access_token(creds_path)
    if token is None:
        return ClaudeQuotaStatus(checked_at=time.monotonic(), error="no-credentials")

    fetcher = _fetch if callable(_fetch) else _fetch_usage
    _cached_status = fetcher(token)
    return _cached_status


def claude_quota_block_reason(
    *,
    creds_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> str | None:
    """Return a blocking reason string if Claude quota is reached, or None."""
    status = check_claude_quota(creds_path=creds_path, cache_ttl=cache_ttl, _fetch=_fetch)
    if status.error:
        return None  # fail-open
    if status.limit_reached:
        return (
            f"claude usage limit reached "
            f"(long-term window at {status.long_term.used_percent:.0f}%, resets {status.long_term.reset_at})"
        )
    return None


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
