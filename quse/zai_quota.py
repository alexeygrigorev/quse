"""Proactive Z.AI quota checking via the Z.AI monitor API."""

from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
from pathlib import Path
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from quse._shared import UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60
_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "goz" / "config.json"
_DEFAULT_ZAI_BASE_URL = "https://api.z.ai/api/anthropic"
_DEFAULT_TIMEOUT_SECONDS = 120.0


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _float_timeout(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return _DEFAULT_TIMEOUT_SECONDS


def _monitor_base(url: str) -> str:
    if "/api/anthropic" in url:
        url = url.replace("/api/anthropic", "")
    return url.rstrip("/")


@dataclass(slots=True)
class ZaiConfig:
    token: str
    base_url: str = _DEFAULT_ZAI_BASE_URL
    timeout: float = _DEFAULT_TIMEOUT_SECONDS


@dataclass(slots=True)
class ZaiQuotaWindow:
    used_percent: float = 0.0
    window_hours: int | None = None
    remaining: int | None = None
    limit: int | None = None
    reset_at: datetime | None = None

    def __post_init__(self) -> None:
        self.used_percent = float(self.used_percent)

    @property
    def percent_remaining(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass(slots=True, init=False)
class ZaiQuotaStatus:
    five_hour: ZaiQuotaWindow = field(default_factory=ZaiQuotaWindow)
    weekly: ZaiQuotaWindow = field(default_factory=ZaiQuotaWindow)
    monthly_web_search: ZaiQuotaWindow = field(default_factory=ZaiQuotaWindow)
    limit_reached: bool = False
    checked_at: float = 0.0
    error: str | None = None

    def __init__(
        self,
        *,
        five_hour: ZaiQuotaWindow | None = None,
        weekly: ZaiQuotaWindow | None = None,
        monthly_web_search: ZaiQuotaWindow | None = None,
        api_calls: ZaiQuotaWindow | None = None,
        tokens: ZaiQuotaWindow | None = None,
        limit_reached: bool = False,
        checked_at: float = 0.0,
        error: str | None = None,
    ) -> None:
        self.five_hour = five_hour or api_calls or ZaiQuotaWindow()
        self.weekly = weekly or tokens or ZaiQuotaWindow()
        self.monthly_web_search = monthly_web_search or ZaiQuotaWindow()
        self.limit_reached = limit_reached
        self.checked_at = checked_at
        self.error = error

    @property
    def api_calls(self) -> ZaiQuotaWindow:
        return self.five_hour

    @property
    def tokens(self) -> ZaiQuotaWindow:
        return self.weekly

    @property
    def max_used_percent(self) -> float:
        return max(self.five_hour.used_percent, self.weekly.used_percent)

    @property
    def short_term(self) -> UsageWindow:
        return UsageWindow(
            percent_remaining=self.five_hour.percent_remaining,
            reset_at=self.five_hour.reset_at,
            window="5h",
        )

    @property
    def long_term(self) -> UsageWindow:
        return UsageWindow(
            percent_remaining=self.weekly.percent_remaining,
            reset_at=self.weekly.reset_at,
            window="weekly",
        )


_cached_status: ZaiQuotaStatus | None = None


def _read_zai_config(config_path: Path | None = None) -> ZaiConfig:
    path = _DEFAULT_CONFIG_PATH
    if config_path is not None:
        path = config_path
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    token = data.get("zai_token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("zai_token is missing")
    base_url = data.get("zai_base_url", _DEFAULT_ZAI_BASE_URL)
    if not isinstance(base_url, str) or not base_url.strip():
        base_url = _DEFAULT_ZAI_BASE_URL
    return ZaiConfig(
        token=token,
        base_url=base_url,
        timeout=_float_timeout(data.get("timeout")),
    )


def _fetch_quota_limit(config: ZaiConfig) -> dict:
    url = f"{_monitor_base(config.base_url)}/api/monitor/usage/quota/limit"
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=config.timeout) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    if isinstance(data, dict):
        return data
    return {}


def _parse_usage_response(data: dict) -> ZaiQuotaStatus:
    five_hour = ZaiQuotaWindow()
    weekly = ZaiQuotaWindow()
    monthly_web_search = ZaiQuotaWindow()
    found_five_hour = False
    found_weekly = False

    if isinstance(data.get("data"), dict):
        data = data["data"]
    limits = data.get("limits")
    if not isinstance(limits, list):
        limits = []
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        window = ZaiQuotaWindow(
            used_percent=limit.get("percentage", 0),
            window_hours=_int_or_none(limit.get("window_hours", limit.get("unit"))),
            remaining=_int_or_none(limit.get("remaining")),
            limit=_int_or_none(limit.get("limit", limit.get("usage"))),
            reset_at=normalize_reset_at(
                limit.get("reset_at", limit.get("nextResetTime"))
            ),
        )
        limit_type = limit.get("type")
        unit = window.window_hours
        if limit_type == "TOKENS_LIMIT" and unit == 3:
            window.window_hours = 5
            window.reset_at = None
            five_hour = window
            found_five_hour = True
        elif limit_type == "TOKENS_LIMIT" and unit == 6:
            window.window_hours = None
            weekly = window
            found_weekly = True
        elif limit_type == "TIME_LIMIT" and unit == 5:
            monthly_web_search = window
        elif limit_type == "TIME_LIMIT" and not found_five_hour:
            five_hour = window
            found_five_hour = True
        elif limit_type == "TOKENS_LIMIT" and not found_weekly:
            weekly = window
            found_weekly = True

    return ZaiQuotaStatus(
        five_hour=five_hour,
        weekly=weekly,
        monthly_web_search=monthly_web_search,
        limit_reached=five_hour.used_percent >= 100.0 or weekly.used_percent >= 100.0,
        checked_at=time.monotonic(),
    )


def _fetch_usage(*, config_path: Path | None = None) -> ZaiQuotaStatus:
    try:
        config = _read_zai_config(config_path)
        data = _fetch_quota_limit(config)
    except (
        FileNotFoundError,
        ValueError,
        json.JSONDecodeError,
        OSError,
        URLError,
        TimeoutError,
    ) as exc:
        logger.warning("zai quota check failed (fail-open): %s", exc)
        return ZaiQuotaStatus(checked_at=time.monotonic(), error=str(exc))
    return _parse_usage_response(data)


def check_zai_quota(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    config_path: Path | None = None,
    _fetch: object = None,
) -> ZaiQuotaStatus:
    """Check Z.AI quota directly. Returns cached result within TTL. Fails open."""
    global _cached_status
    if (
        _cached_status is not None
        and time.monotonic() - _cached_status.checked_at < cache_ttl
    ):
        return _cached_status

    fetcher = _fetch_usage
    if callable(_fetch):
        fetcher = _fetch
    if callable(_fetch):
        _cached_status = fetcher()
    else:
        _cached_status = fetcher(config_path=config_path)
    return _cached_status


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
