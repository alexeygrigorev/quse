"""Proactive Copilot quota checking via GitHub API."""

from dataclasses import dataclass
import json
import logging
import subprocess
import time

from quse._shared import UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None


@dataclass(slots=True)
class CopilotQuotaStatus:
    premium_remaining: int | None = None
    premium_entitlement: int | None = None
    premium_percent_remaining: float = 100.0
    quota_reset_date: str | None = None
    checked_at: float = 0.0
    error: str | None = None

    def __post_init__(self) -> None:
        self.premium_percent_remaining = float(self.premium_percent_remaining)
        self.quota_reset_date = normalize_reset_at(self.quota_reset_date)

    @property
    def used_percent(self) -> float:
        return max(0.0, 100.0 - self.premium_percent_remaining)

    @property
    def limit_reached(self) -> bool:
        return self.error is None and self.premium_percent_remaining <= 20.0

    @property
    def short_term(self) -> UsageWindow:
        return UsageWindow(percent_remaining=100.0)

    @property
    def long_term(self) -> UsageWindow:
        return UsageWindow(percent_remaining=self.premium_percent_remaining, reset_at=self.quota_reset_date)


_cached_status: CopilotQuotaStatus | None = None


def _fetch_quota(*, timeout: float = 10.0) -> CopilotQuotaStatus:
    try:
        result = subprocess.run(
            ["gh", "api", "/copilot_internal/user"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return CopilotQuotaStatus(checked_at=time.monotonic(), error=f"gh exit {result.returncode}")
        data = json.loads(result.stdout)
    except FileNotFoundError:
        return CopilotQuotaStatus(checked_at=time.monotonic(), error="gh not on PATH")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("copilot quota check failed (fail-open): %s", exc)
        return CopilotQuotaStatus(checked_at=time.monotonic(), error=str(exc))

    snapshots = data.get("quota_snapshots")
    if not isinstance(snapshots, dict):
        snapshots = {}
    premium = snapshots.get("premium_interactions")
    if not isinstance(premium, dict):
        premium = {}

    if premium.get("unlimited", False):
        return CopilotQuotaStatus(checked_at=time.monotonic())

    return CopilotQuotaStatus(
        premium_remaining=_int_or_none(premium.get("remaining")),
        premium_entitlement=_int_or_none(premium.get("entitlement")),
        premium_percent_remaining=max(0.0, float(premium.get("percent_remaining", 100.0))),
        quota_reset_date=data.get("quota_reset_date"),
        checked_at=time.monotonic(),
    )


def check_copilot_quota(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> CopilotQuotaStatus:
    """Check Copilot quota via gh CLI. Returns cached result within TTL. Fails open."""
    global _cached_status
    if _cached_status is not None and time.monotonic() - _cached_status.checked_at < cache_ttl:
        return _cached_status

    fetcher = _fetch_quota
    if callable(_fetch):
        fetcher = _fetch
    _cached_status = fetcher()
    return _cached_status


def copilot_quota_block_reason(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> str | None:
    """Return a blocking reason if Copilot quota is reached, or None."""
    status = check_copilot_quota(cache_ttl=cache_ttl, _fetch=_fetch)
    if status.error:
        return None  # fail-open
    if status.limit_reached:
        return (
            f"copilot premium requests low "
            f"({status.long_term.percent_remaining:.0f}% remaining, resets {status.long_term.reset_at})"
        )
    return None


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
