"""Proactive Z.AI quota checking via the Z.AI monitor API."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
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


def _normalize_zai_reset_at(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed_reset = normalize_reset_at(value)
    if parsed_reset is not None:
        return parsed_reset
    normalized = str(value).strip()
    if not normalized.isdigit():
        return None
    return _normalize_zai_reset_at(int(normalized))


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
    reset_at: str | None = None

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
        return UsageWindow(
            percent_remaining=self.api_calls.percent_remaining,
            reset_at=self.api_calls.reset_at,
        )

    @property
    def long_term(self) -> UsageWindow:
        return UsageWindow(
            percent_remaining=self.tokens.percent_remaining,
            reset_at=self.tokens.reset_at,
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
    api_calls = ZaiQuotaWindow()
    tokens = ZaiQuotaWindow()

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
            reset_at=_normalize_zai_reset_at(limit.get("reset_at", limit.get("nextResetTime"))),
        )
        if limit.get("type") == "TIME_LIMIT":
            api_calls = window
        if limit.get("type") == "TOKENS_LIMIT":
            tokens = window

    return ZaiQuotaStatus(api_calls=api_calls, tokens=tokens, checked_at=time.monotonic())


def _fetch_usage(*, config_path: Path | None = None) -> ZaiQuotaStatus:
    try:
        config = _read_zai_config(config_path)
        data = _fetch_quota_limit(config)
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError, URLError, TimeoutError) as exc:
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
    if _cached_status is not None and time.monotonic() - _cached_status.checked_at < cache_ttl:
        return _cached_status

    fetcher = _fetch_usage
    if callable(_fetch):
        fetcher = _fetch
    if callable(_fetch):
        _cached_status = fetcher()
    else:
        _cached_status = fetcher(config_path=config_path)
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
