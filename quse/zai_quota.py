"""Proactive Z.AI quota checking via goz CLI."""

from dataclasses import dataclass, field
import json
import logging
import subprocess
import time

from quse._shared import UsageWindow

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60


@dataclass(slots=True)
class ZaiQuotaWindow:
    used_percent: float = 0.0
    window_hours: int | None = None
    remaining: int | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        self.used_percent = float(self.used_percent)

    @property
    def percent_remaining(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass(slots=True)
class ZaiQuotaStatus:
    api_calls: ZaiQuotaWindow = field(default_factory=ZaiQuotaWindow)
    tokens: ZaiQuotaWindow = field(default_factory=ZaiQuotaWindow)
    limit_reached: bool = False
    checked_at: float = 0.0
    error: str | None = None

    @property
    def max_used_percent(self) -> float:
        return max(self.api_calls.used_percent, self.tokens.used_percent)

    @property
    def short_term(self) -> UsageWindow:
        return UsageWindow(percent_remaining=self.tokens.percent_remaining)

    @property
    def long_term(self) -> UsageWindow:
        return UsageWindow(percent_remaining=100.0)


_cached_status: ZaiQuotaStatus | None = None


def _fetch_usage(*, timeout: float = 10.0) -> ZaiQuotaStatus:
    try:
        result = subprocess.run(
            ["goz", "usage", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return ZaiQuotaStatus(checked_at=time.monotonic(), error=f"goz exit {result.returncode}")
        data = json.loads(result.stdout)
    except FileNotFoundError:
        return ZaiQuotaStatus(checked_at=time.monotonic(), error="goz not on PATH")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("zai quota check failed (fail-open): %s", exc)
        return ZaiQuotaStatus(checked_at=time.monotonic(), error=str(exc))

    api_calls = ZaiQuotaWindow()
    tokens = ZaiQuotaWindow()

    limits = data.get("limits")
    if not isinstance(limits, list):
        limits = []
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        window = ZaiQuotaWindow(
            used_percent=limit.get("percentage", 0),
            window_hours=limit.get("window_hours") if isinstance(limit.get("window_hours"), int) else None,
            remaining=limit.get("remaining") if isinstance(limit.get("remaining"), int) else None,
            limit=limit.get("limit") if isinstance(limit.get("limit"), int) else None,
        )
        if limit.get("type") == "TIME_LIMIT":
            api_calls = window
        if limit.get("type") == "TOKENS_LIMIT":
            tokens = window

    return ZaiQuotaStatus(api_calls=api_calls, tokens=tokens, checked_at=time.monotonic())


def check_zai_quota(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> ZaiQuotaStatus:
    """Check Z.AI quota via goz CLI. Returns cached result within TTL. Fails open."""
    global _cached_status
    if _cached_status is not None and time.monotonic() - _cached_status.checked_at < cache_ttl:
        return _cached_status

    fetcher = _fetch if callable(_fetch) else _fetch_usage
    _cached_status = fetcher()
    return _cached_status


def zai_quota_block_reason(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> str | None:
    """Return a blocking reason if Z.AI quota is reached, or None."""
    status = check_zai_quota(cache_ttl=cache_ttl, _fetch=_fetch)
    if status.error:
        return None  # fail-open
    return None


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
