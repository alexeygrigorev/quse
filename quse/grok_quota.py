"""Proactive Grok Build quota checking via the cli-chat-proxy billing API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
import json
import logging
import os
import time
import urllib.error
import urllib.request

from quse._shared import UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
_DEFAULT_OIDC_ISSUER = "https://auth.x.ai"
_CACHE_TTL_SECONDS = 60
_TOKEN_REFRESH_BUFFER = timedelta(minutes=5)
_DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(slots=True)
class GrokQuotaWindow:
    used_percent: float | None = None
    reset_at: datetime | None = None
    present: bool = False
    limit: float | None = None
    used: float | None = None

    def __post_init__(self) -> None:
        if self.used_percent is not None:
            self.used_percent = float(self.used_percent)
        if self.limit is not None:
            self.limit = float(self.limit)
        if self.used is not None:
            self.used = float(self.used)
        self.reset_at = normalize_reset_at(self.reset_at)

    @property
    def percent_remaining(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, 100.0 - self.used_percent)


@dataclass(slots=True)
class GrokQuotaStatus:
    weekly: GrokQuotaWindow = field(default_factory=GrokQuotaWindow)
    monthly: GrokQuotaWindow = field(default_factory=GrokQuotaWindow)
    limit_reached: bool = False
    checked_at: float = 0.0
    error: str | None = None
    subscription: str | None = None
    has_grok_code_access: bool | None = None
    is_unified_billing_user: bool | None = None
    prepaid_balance: float | None = None
    on_demand_cap: float | None = None
    on_demand_used: float | None = None
    product_usage: list[dict[str, float | str]] = field(default_factory=list)

    @property
    def short_term(self) -> UsageWindow | None:
        window = _window_for_term(self.weekly, self.monthly, long_term=False)
        return _as_usage_window(window, monthly=self.monthly)

    @property
    def long_term(self) -> UsageWindow | None:
        window = _window_for_term(self.weekly, self.monthly, long_term=True)
        return _as_usage_window(window, monthly=self.monthly)


@dataclass(slots=True)
class GrokOAuthCredentials:
    path: Path
    data: dict
    entry_key: str
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None


_cached_status: GrokQuotaStatus | None = None


def _as_usage_window(
    window: GrokQuotaWindow | None,
    *,
    monthly: GrokQuotaWindow,
) -> UsageWindow | None:
    if window is None or not window.present:
        return None
    label = "weekly"
    if window is monthly:
        label = "monthly"
    return UsageWindow(
        percent_remaining=window.percent_remaining,
        reset_at=window.reset_at,
        window=label,
    )


def _window_for_term(
    weekly: GrokQuotaWindow,
    monthly: GrokQuotaWindow,
    *,
    long_term: bool,
) -> GrokQuotaWindow | None:
    """Select a short- or long-term window from Grok's active windows.

    With two windows Grok exposes weekly first and monthly second. With one
    window, that sole window is the longer (weekly or monthly) allowance.
    """
    windows = [window for window in (weekly, monthly) if window.present]
    if len(windows) == 1:
        if long_term:
            return windows[0]
        return None
    if len(windows) >= 2:
        if long_term:
            return windows[1]
        return windows[0]
    return None


def _default_auth_path() -> Path:
    config_dir = os.environ.get("GROK_HOME")
    if config_dir:
        return Path(config_dir) / "auth.json"
    return Path.home() / ".grok" / "auth.json"


def _default_base_url() -> str:
    configured = os.environ.get("GROK_CLI_CHAT_PROXY_BASE_URL")
    if isinstance(configured, str) and configured.strip():
        return configured.rstrip("/")
    return _DEFAULT_BASE_URL


def _unwrap_val(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, dict):
        return None
    inner = value.get("val")
    if isinstance(inner, bool):
        return None
    if isinstance(inner, (int, float)):
        return float(inner)
    return None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _config_object(data: dict) -> dict:
    config = data.get("config")
    if isinstance(config, dict):
        return config
    return data


def _clamp_percent(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return value


def _parse_product_usage(config: dict) -> list[dict[str, float | str]]:
    raw = config.get("productUsage")
    if not isinstance(raw, list):
        return []
    parsed: list[dict[str, float | str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        product = item.get("product")
        percent = item.get("usagePercent")
        if not isinstance(product, str) or not product:
            continue
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        parsed.append(
            {
                "product": product,
                "usage_percent": _clamp_percent(float(percent)),
            }
        )
    return parsed


def _product_used_percent(
    products: list[dict[str, float | str]], name: str
) -> float | None:
    for item in products:
        if item.get("product") != name:
            continue
        percent = item.get("usage_percent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        return float(percent)
    return None


def _weekly_used_percent(
    config: dict, products: list[dict[str, float | str]]
) -> float | None:
    product_percent = _product_used_percent(products, "GrokBuild")
    if product_percent is not None:
        return product_percent
    reported = config.get("creditUsagePercent")
    if isinstance(reported, bool):
        reported = None
    if isinstance(reported, (int, float)):
        return _clamp_percent(float(reported))
    cap = _unwrap_val(config.get("onDemandCap"))
    used = _unwrap_val(config.get("onDemandUsed"))
    if cap is None or used is None:
        return None
    if cap <= 0:
        return None
    return _clamp_percent((used / cap) * 100.0)


def _monthly_used_percent(used: float | None, limit: float | None) -> float | None:
    if used is None or limit is None:
        return None
    if limit <= 0:
        return None
    return _clamp_percent((used / limit) * 100.0)


def _parse_weekly_window(
    config: dict, products: list[dict[str, float | str]]
) -> GrokQuotaWindow:
    current_period = config.get("currentPeriod")
    period_type = None
    period_end = None
    if isinstance(current_period, dict):
        period_type = current_period.get("type")
        period_end = current_period.get("end")
    used_percent = _weekly_used_percent(config, products)
    is_weekly = period_type == "USAGE_PERIOD_TYPE_WEEKLY"
    if not is_weekly and used_percent is None:
        return GrokQuotaWindow(present=False)
    if period_end is None:
        period_end = config.get("billingPeriodEnd")
    return GrokQuotaWindow(
        used_percent=used_percent,
        reset_at=period_end,
        present=True,
        limit=_unwrap_val(config.get("onDemandCap")),
        used=_unwrap_val(config.get("onDemandUsed")),
    )


def _parse_monthly_window(config: dict) -> GrokQuotaWindow:
    limit = _unwrap_val(config.get("monthlyLimit"))
    used = _unwrap_val(config.get("used"))
    if limit is None or limit <= 0:
        return GrokQuotaWindow(present=False)
    return GrokQuotaWindow(
        used_percent=_monthly_used_percent(used, limit),
        reset_at=config.get("billingPeriodEnd"),
        present=True,
        limit=limit,
        used=used,
    )


def _window_exhausted(window: GrokQuotaWindow) -> bool:
    if not window.present:
        return False
    if window.used_percent is None:
        return False
    return window.used_percent >= 100.0


def _parse_billing_payloads(
    *,
    monthly: dict,
    credits: dict,
    user: dict | None = None,
) -> GrokQuotaStatus:
    monthly_config = _config_object(monthly)
    credits_config = _config_object(credits)
    product_usage = _parse_product_usage(credits_config)
    weekly = _parse_weekly_window(credits_config, product_usage)
    monthly_window = _parse_monthly_window(monthly_config)

    subscription = None
    has_grok_code_access = None
    if isinstance(user, dict):
        subscription = _optional_string(user.get("subscriptionTier"))
        has_grok_code_access = _optional_bool(user.get("hasGrokCodeAccess"))

    return GrokQuotaStatus(
        weekly=weekly,
        monthly=monthly_window,
        limit_reached=_window_exhausted(weekly) or _window_exhausted(monthly_window),
        checked_at=time.monotonic(),
        subscription=subscription,
        has_grok_code_access=has_grok_code_access,
        is_unified_billing_user=_optional_bool(
            credits_config.get("isUnifiedBillingUser")
        ),
        prepaid_balance=_unwrap_val(credits_config.get("prepaidBalance")),
        on_demand_cap=_unwrap_val(credits_config.get("onDemandCap")),
        on_demand_used=_unwrap_val(credits_config.get("onDemandUsed")),
        product_usage=product_usage,
    )


def _read_oauth_credentials(
    auth_path: Path | None = None,
) -> GrokOAuthCredentials | None:
    path = auth_path or _default_auth_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("grok auth.json not found at %s", path)
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("grok auth.json parse error: %s", exc)
        return None
    if not isinstance(data, dict):
        logger.warning("grok auth.json is not an object")
        return None

    candidates: list[GrokOAuthCredentials] = []
    for entry_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        token = entry.get("key")
        if not isinstance(token, str) or not token:
            continue
        refresh_token = entry.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = None
        candidates.append(
            GrokOAuthCredentials(
                path=path,
                data=data,
                entry_key=str(entry_key),
                access_token=token,
                refresh_token=refresh_token,
                expires_at=normalize_reset_at(entry.get("expires_at")),
                oidc_issuer=_optional_string(entry.get("oidc_issuer")),
                oidc_client_id=_optional_string(entry.get("oidc_client_id")),
            )
        )
    if not candidates:
        logger.warning("grok auth.json missing session token")
        return None
    return _select_credentials(candidates)


def _select_credentials(
    candidates: list[GrokOAuthCredentials],
) -> GrokOAuthCredentials:
    now = datetime.now(timezone.utc)
    fresh: list[GrokOAuthCredentials] = []
    refreshable: list[GrokOAuthCredentials] = []
    for cred in candidates:
        if cred.expires_at is None or cred.expires_at > now:
            fresh.append(cred)
        if cred.refresh_token:
            refreshable.append(cred)
    if fresh:
        return fresh[0]
    if refreshable:
        return refreshable[0]
    return candidates[0]


def _oauth_token_expires_soon(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    return datetime.now(timezone.utc) + _TOKEN_REFRESH_BUFFER >= expires_at


def _token_endpoint(issuer: str | None) -> str:
    base = issuer or _DEFAULT_OIDC_ISSUER
    return f"{base.rstrip('/')}/oauth2/token"


def _write_oauth_credentials(credentials: GrokOAuthCredentials) -> None:
    tmp_path = credentials.path.with_suffix(f"{credentials.path.suffix}.tmp")
    tmp_path.write_text(
        json.dumps(credentials.data, indent=2) + "\n", encoding="utf-8"
    )
    tmp_path.replace(credentials.path)
    try:
        os.chmod(credentials.path, 0o600)
    except OSError:
        pass


def _refresh_access_token(
    credentials: GrokOAuthCredentials, *, timeout: float = 30.0
) -> str:
    if not credentials.refresh_token:
        raise ValueError("grok refresh token is missing")
    if not credentials.oidc_client_id:
        raise ValueError("grok oidc_client_id is missing")
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": credentials.refresh_token,
            "client_id": credentials.oidc_client_id,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _token_endpoint(credentials.oidc_issuer),
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        refreshed = json.loads(response.read().decode("utf-8"))
    if not isinstance(refreshed, dict):
        raise ValueError("grok oauth refresh response is not an object")

    access_token = refreshed.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("grok oauth refresh response missing access_token")

    entry = credentials.data.get(credentials.entry_key)
    if not isinstance(entry, dict):
        raise ValueError("grok credentials entry is not an object")
    entry["key"] = access_token
    refresh_token = refreshed.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        entry["refresh_token"] = refresh_token
    expires_in = refreshed.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expiry = datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
        entry["expires_at"] = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_oauth_credentials(credentials)
    return access_token


def _read_access_token(auth_path: Path | None = None) -> str | None:
    credentials = _read_oauth_credentials(auth_path)
    if credentials is None:
        return None
    if _oauth_token_expires_soon(credentials.expires_at):
        try:
            return _refresh_access_token(credentials)
        except (
            ValueError,
            json.JSONDecodeError,
            OSError,
            urllib.error.URLError,
            TimeoutError,
        ) as exc:
            logger.warning(
                "grok oauth refresh failed; using stored access token: %s", exc
            )
    return credentials.access_token


def _fetch_json(url: str, token: str, *, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "x-grok-client-mode": "cli",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    return data


def _fetch_quota(
    token: str,
    *,
    base_url: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> GrokQuotaStatus:
    root = base_url or _default_base_url()
    monthly: dict = {}
    credits: dict = {}
    monthly_error: Exception | None = None
    credits_error: Exception | None = None
    try:
        monthly = _fetch_json(f"{root}/billing", token, timeout=timeout)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        ValueError,
        OSError,
    ) as exc:
        monthly_error = exc
        logger.warning("grok monthly billing check failed: %s", exc)
    try:
        credits = _fetch_json(f"{root}/billing?format=credits", token, timeout=timeout)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        ValueError,
        OSError,
    ) as exc:
        credits_error = exc
        logger.warning("grok weekly billing check failed: %s", exc)
    if monthly_error is not None and credits_error is not None:
        return GrokQuotaStatus(checked_at=time.monotonic(), error=str(monthly_error))

    user = None
    try:
        user = _fetch_json(f"{root}/user?include=subscription", token, timeout=timeout)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        ValueError,
        OSError,
    ) as exc:
        logger.warning("grok user/subscription check failed (non-blocking): %s", exc)

    return _parse_billing_payloads(monthly=monthly, credits=credits, user=user)


def check_grok_quota(
    *,
    auth_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> GrokQuotaStatus:
    """Check Grok Build quota proactively. Returns cached result within TTL.

    Fails open: if auth is missing or API call fails, returns a non-blocking status.
    """
    global _cached_status
    if (
        _cached_status is not None
        and time.monotonic() - _cached_status.checked_at < cache_ttl
    ):
        return _cached_status

    token = _read_access_token(auth_path)
    if token is None:
        status = GrokQuotaStatus(checked_at=time.monotonic(), error="no-credentials")
        _cached_status = status
        return status

    fetcher = _fetch_quota
    if callable(_fetch):
        fetcher = _fetch
    _cached_status = fetcher(token)
    return _cached_status


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
