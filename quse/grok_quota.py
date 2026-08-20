"""Proactive Grok Build quota checking via the cli-chat-proxy billing API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import http.client
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

from quse._shared import UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
_DEFAULT_RESET_BASE_URL = "https://grok.com"
_DEFAULT_OIDC_ISSUER = "https://auth.x.ai"
_CACHE_TTL_SECONDS = 60
_TOKEN_REFRESH_BUFFER = timedelta(minutes=5)
_DEFAULT_TIMEOUT_SECONDS = 10.0
_RESET_COOKIE_ENV = "GROK_RESET_COOKIE"
_RESET_COOKIE_FILE_ENV = "GROK_RESET_COOKIE_FILE"
_RESET_USER_AGENT_ENV = "GROK_RESET_USER_AGENT"
_RESET_CURL_FILE_ENV = "GROK_RESET_CURL_FILE"
_DEFAULT_RESET_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
_RESET_RPC_BASES = (
    "/prod_mc_billing.ConsumerUiSvc",
    "/grok_api_v2.ConsumerUiSvc",
)


def _ipv4_create_connection(
    address: tuple[str, int],
    timeout: float | None = None,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Open a TCP connection using only IPv4 address candidates.

    Cloudflare currently serves grok.com over IPv6 with a challenge response
    while the same authenticated web RPC is reachable over IPv4. Keep this
    workaround local to the reset RPC instead of changing networking for the
    CLI billing API.
    """
    host, port = address
    errors: list[OSError] = []
    for family, socktype, protocol, _canonname, sockaddr in socket.getaddrinfo(
        host, port, socket.AF_INET, socket.SOCK_STREAM
    ):
        sock = socket.socket(family, socktype, protocol)
        try:
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address is not None:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            errors.append(exc)
            sock.close()
    if errors:
        raise errors[-1]
    raise OSError(f"no IPv4 address found for {host}")


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that avoids the broken IPv6 Cloudflare route."""

    _create_connection = staticmethod(_ipv4_create_connection)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_IPv4HTTPSConnection, request, context=self._context)


_RESET_IPV4_OPENER = urllib.request.build_opener(_IPv4HTTPSHandler())


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
class GrokReset:
    """A one-time Grok usage-limit reset returned by ``GetRemainingResets``."""

    token_id: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.token_id, str):
            self.token_id = self.token_id.strip() or None
        else:
            self.token_id = None
        self.expires_at = normalize_reset_at(self.expires_at)

    @property
    def is_available(self) -> bool:
        if self.token_id is None:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > datetime.now(timezone.utc)

    @property
    def validity_end(self) -> datetime | None:
        """Compatibility name matching Grok's ``validityEnd`` API field."""
        return self.expires_at


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
    resets: list[GrokReset] = field(default_factory=list)
    resets_error: str | None = None

    @property
    def short_term(self) -> UsageWindow | None:
        window = _window_for_term(self.weekly, self.monthly, long_term=False)
        return _as_usage_window(window, monthly=self.monthly)

    @property
    def long_term(self) -> UsageWindow | None:
        window = _window_for_term(self.weekly, self.monthly, long_term=True)
        return _as_usage_window(window, monthly=self.monthly)

    @property
    def available_resets(self) -> list[GrokReset]:
        return [reset for reset in self.resets if reset.is_available]

    @property
    def reset_credits(self) -> list[GrokReset]:
        """Alias for callers that use Codex's reset-credit terminology."""
        return self.resets

    @property
    def available_reset_credits(self) -> list[GrokReset]:
        """Alias for callers that use Codex's reset-credit terminology."""
        return self.available_resets


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


def _parse_reset_item(item: object) -> GrokReset | None:
    if not isinstance(item, dict):
        return None
    token_id = item.get("tokenId")
    if token_id is None:
        token_id = item.get("token_id")
    if token_id is None:
        token_id = item.get("id")

    expires_at = item.get("validityEnd")
    if expires_at is None:
        expires_at = item.get("validity_end")
    if expires_at is None:
        expires_at = item.get("expiresAt")
    if expires_at is None:
        expires_at = item.get("expires_at")

    reset = GrokReset(token_id=token_id, expires_at=_timestamp_value(expires_at))
    if reset.token_id is None:
        return None
    return reset


def _timestamp_value(value: object) -> object:
    if isinstance(value, dict):
        seconds = value.get("seconds")
        nanos = value.get("nanos", 0)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float, str)):
            return None
        if isinstance(nanos, bool) or not isinstance(nanos, (int, float, str)):
            nanos = 0
        try:
            return float(seconds) + (float(nanos) / 1_000_000_000)
        except (TypeError, ValueError):
            return None
    return value


def _reset_items_from_json(data: object) -> list[GrokReset]:
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("tokens")
        if not isinstance(raw_items, list):
            raw_items = data.get("stillRedeemable")
        if not isinstance(raw_items, list):
            raw_items = data.get("still_redeemable")
        if not isinstance(raw_items, list):
            raw_items = data.get("resets")
        if not isinstance(raw_items, list):
            raw_items = []
    else:
        return []

    parsed: list[GrokReset] = []
    seen: set[tuple[str | None, datetime | None]] = set()
    for item in raw_items:
        reset = _parse_reset_item(item)
        if reset is None:
            continue
        key = (reset.token_id, reset.expires_at)
        if key in seen:
            continue
        seen.add(key)
        parsed.append(reset)
    return parsed


def _decode_varint(data: bytes, offset: int) -> tuple[int, int] | None:
    value = 0
    shift = 0
    position = offset
    while position < len(data) and shift <= 63:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, position
        shift += 7
    return None


def _iter_wire_fields(data: bytes):
    position = 0
    while position < len(data):
        tag_result = _decode_varint(data, position)
        if tag_result is None:
            return
        tag, position = tag_result
        field = tag >> 3
        wire_type = tag & 0x07
        if field <= 0:
            return
        if wire_type == 0:
            value_result = _decode_varint(data, position)
            if value_result is None:
                return
            value, position = value_result
            yield field, wire_type, value
        elif wire_type == 1:
            end = position + 8
            if end > len(data):
                return
            yield field, wire_type, data[position:end]
            position = end
        elif wire_type == 2:
            length_result = _decode_varint(data, position)
            if length_result is None:
                return
            length, position = length_result
            end = position + length
            if end > len(data):
                return
            yield field, wire_type, data[position:end]
            position = end
        elif wire_type == 5:
            end = position + 4
            if end > len(data):
                return
            yield field, wire_type, data[position:end]
            position = end
        else:
            return


def _parse_timestamp_message(data: bytes) -> datetime | None:
    seconds: int | None = None
    nanos = 0
    for field_number, wire_type, value in _iter_wire_fields(data):
        if wire_type != 0:
            continue
        if field_number == 1:
            seconds = int(value)
        elif field_number == 2:
            nanos = int(value)
    if seconds is not None:
        return normalize_reset_at(float(seconds) + (float(nanos) / 1_000_000_000))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return normalize_reset_at(text)


def _parse_reset_token_message(data: bytes) -> GrokReset | None:
    token_id: str | None = None
    expires_at: datetime | None = None
    for field_number, wire_type, value in _iter_wire_fields(data):
        if wire_type != 2:
            continue
        if field_number in (1, 10):
            try:
                candidate = value.decode("utf-8").strip()
            except UnicodeDecodeError:
                candidate = ""
            if candidate and token_id is None:
                token_id = candidate
        elif field_number in (2, 3, 20, 30):
            timestamp = _parse_timestamp_message(value)
            if timestamp is not None:
                expires_at = timestamp
    if token_id is None:
        return None
    return GrokReset(token_id=token_id, expires_at=expires_at)


def _walk_reset_messages(data: bytes, parsed: list[GrokReset]) -> None:
    for field_number, wire_type, value in _iter_wire_fields(data):
        if wire_type != 2 or field_number not in (1, 10):
            continue
        reset = _parse_reset_token_message(value)
        if reset is not None:
            parsed.append(reset)
        else:
            _walk_reset_messages(value, parsed)


def _grpc_payloads(data: bytes) -> list[bytes]:
    if not data:
        return []
    payloads: list[bytes] = []
    position = 0
    while position + 5 <= len(data):
        flag = data[position]
        length = int.from_bytes(data[position + 1 : position + 5], "big")
        end = position + 5 + length
        if end > len(data):
            break
        if flag & 0x80 == 0:
            payloads.append(data[position + 5 : end])
        position = end
    if payloads and position == len(data):
        return payloads
    return [data]


def _parse_grpc_resets_response(data: bytes) -> list[GrokReset]:
    parsed: list[GrokReset] = []
    for payload in _grpc_payloads(data):
        _walk_reset_messages(payload, parsed)
    unique: list[GrokReset] = []
    seen: set[tuple[str | None, datetime | None]] = set()
    for reset in parsed:
        key = (reset.token_id, reset.expires_at)
        if key in seen:
            continue
        seen.add(key)
        unique.append(reset)
    return unique


def _parse_resets_response(data: object) -> list[GrokReset]:
    """Parse JSON or gRPC-web ``GetRemainingResets`` responses."""
    if isinstance(data, dict) or isinstance(data, list):
        return _reset_items_from_json(data)
    if isinstance(data, bytes):
        stripped = data.lstrip()
        if stripped.startswith(b"{") or stripped.startswith(b"["):
            try:
                return _reset_items_from_json(json.loads(data.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return []
        return _parse_grpc_resets_response(data)
    return []


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


def _normalise_cookie_header(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    if not value or "\r" in value or "\n" in value:
        return None
    return value


def _curl_headers(value: object) -> dict[str, str]:
    """Extract the useful request headers from a browser's copied cURL."""
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text or not text.lower().startswith("curl"):
        return {}
    try:
        args = shlex.split(text)
    except ValueError:
        return {}
    if not args or not args[0].lower().endswith("curl"):
        return {}

    headers: dict[str, str] = {}
    index = 1
    while index < len(args):
        argument = args[index]
        header_value: str | None = None
        if argument in ("-H", "--header"):
            index += 1
            if index < len(args):
                header_value = args[index]
        elif argument.startswith("--header="):
            header_value = argument.split("=", 1)[1]
        elif argument in ("-A", "--user-agent"):
            index += 1
            if index < len(args):
                headers["user-agent"] = args[index]
        elif argument.startswith("--user-agent="):
            headers["user-agent"] = argument.split("=", 1)[1]
        elif argument in ("-b", "--cookie"):
            index += 1
            if index < len(args):
                header_value = f"Cookie: {args[index]}"
        elif argument.startswith("--cookie="):
            header_value = f"Cookie: {argument.split('=', 1)[1]}"

        if header_value is not None:
            name, separator, value_text = header_value.partition(":")
            if separator and name.strip():
                key = name.strip().lower()
                value_text = value_text.strip()
                if value_text:
                    if key == "cookie" and headers.get(key):
                        headers[key] = f"{headers[key]}; {value_text}"
                    else:
                        headers[key] = value_text
        index += 1
    return headers


def _cookie_header_from_curl(value: object) -> str | None:
    header = _curl_headers(value).get("cookie")
    return _normalise_cookie_header(header)


def _cookie_header_from_mapping(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    parts: list[str] = []
    for name, cookie_value in value.items():
        if not isinstance(name, str) or not isinstance(cookie_value, str):
            continue
        name = name.strip()
        cookie_value = cookie_value.strip()
        if (
            not name
            or not cookie_value
            or any(char in name for char in "=;\r\n")
            or "\r" in cookie_value
            or "\n" in cookie_value
        ):
            continue
        parts.append(f"{name}={cookie_value}")
    return "; ".join(parts) or None


def _cookie_header_from_value(value: object) -> str | None:
    header = _cookie_header_from_curl(value)
    if header is not None:
        return header
    header = _normalise_cookie_header(value)
    if header is not None:
        return header
    if not isinstance(value, dict):
        if not isinstance(value, list):
            return None
        cookies = {
            item.get("name"): item.get("value")
            for item in value
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)
        }
        return _cookie_header_from_mapping(cookies)

    for key in (
        "cookie",
        "cookie_header",
        "reset_cookie",
        "web_cookie",
        "web_cookies",
        "cookies",
        "curl",
        "curl_command",
        "request",
    ):
        nested = value.get(key)
        header = _cookie_header_from_value(nested)
        if header is not None:
            return header
    return _cookie_header_from_mapping(value)


def _reset_cookie_file_paths() -> list[Path]:
    configured = os.environ.get(_RESET_COOKIE_FILE_ENV)
    paths: list[Path] = []
    if isinstance(configured, str) and configured.strip():
        paths.append(Path(configured).expanduser())
    else:
        configured_curl = os.environ.get(_RESET_CURL_FILE_ENV)
        if isinstance(configured_curl, str) and configured_curl.strip():
            paths.append(Path(configured_curl).expanduser())
        else:
            grok_home = os.environ.get("GROK_HOME")
            if grok_home:
                root = Path(grok_home).expanduser()
            else:
                root = Path.home() / ".grok"
            for name in (
                "cookies.json",
                "browser-cookies.json",
                "reset.curl",
                "grok-reset.curl",
            ):
                paths.append(root / name)
    return paths


def _reset_cookie_from_file() -> str | None:
    for path in _reset_cookie_file_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        try:
            data: object = json.loads(text)
        except json.JSONDecodeError:
            data = text
        header = _cookie_header_from_value(data)
        if header is not None:
            return header
    return None


def _reset_cookie_from_auth(auth_path: Path | None = None) -> str | None:
    path = auth_path or _default_auth_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        header = None
        for key in (
            "cookie",
            "cookie_header",
            "reset_cookie",
            "web_cookie",
            "web_cookies",
            "cookies",
        ):
            nested = entry.get(key)
            header = _normalise_cookie_header(nested)
            if header is None:
                header = _cookie_header_from_mapping(nested)
            if header is not None:
                break
        if header is None:
            header = _cookie_header_from_mapping(
                {
                    key: entry.get(key)
                    for key in (
                        "sso",
                        "sso-rw",
                        "x-anonuserid",
                        "x-challenge",
                        "x-signature",
                        "cf_clearance",
                        "grok_device_id",
                        "__cf_bm",
                    )
                    if key in entry
                }
            )
        if header is not None and ("sso=" in header or "sso-rw=" in header):
            return header
    return None


def _reset_cookie_from_env() -> str | None:
    value = os.environ.get(_RESET_COOKIE_ENV)
    header = _cookie_header_from_value(value)
    if header is not None:
        return header
    return _reset_cookie_from_file()


def _reset_cookie(auth_path: Path | None = None) -> str | None:
    return _reset_cookie_from_env() or _reset_cookie_from_auth(auth_path)


def _reset_user_agent_from_auth(auth_path: Path | None = None) -> str | None:
    path = auth_path or _default_auth_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        for key in ("user_agent", "browser_user_agent", "web_user_agent"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _user_agent_from_value(value: object) -> str | None:
    if isinstance(value, str):
        headers = _curl_headers(value)
        user_agent = headers.get("user-agent")
        if user_agent is not None and user_agent.strip():
            return user_agent.strip()
        return None
    if not isinstance(value, dict):
        return None
    for key in ("user_agent", "browser_user_agent", "web_user_agent"):
        user_agent = value.get(key)
        if isinstance(user_agent, str) and user_agent.strip():
            return user_agent.strip()
    headers = value.get("headers")
    if isinstance(headers, dict):
        for key, user_agent in headers.items():
            if (
                isinstance(key, str)
                and key.lower() == "user-agent"
                and isinstance(user_agent, str)
                and user_agent.strip()
            ):
                return user_agent.strip()
    for key in ("curl", "curl_command", "request"):
        user_agent = _user_agent_from_value(value.get(key))
        if user_agent is not None:
            return user_agent
    return None


def _reset_user_agent_from_file() -> str | None:
    for path in _reset_cookie_file_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        try:
            data: object = json.loads(text)
        except json.JSONDecodeError:
            data = text
        user_agent = _user_agent_from_value(data)
        if user_agent is not None:
            return user_agent
    return None


def _reset_user_agent(auth_path: Path | None = None) -> str | None:
    value = _reset_user_agent_from_env()
    if value is not None:
        return value
    value = _reset_user_agent_from_file()
    return value or _reset_user_agent_from_auth(auth_path)


def _reset_user_agent_from_env() -> str | None:
    value = os.environ.get(_RESET_USER_AGENT_ENV)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _request_origin(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return "https://grok.com"
    return f"{parsed.scheme}://{parsed.netloc}"


def _reset_request_headers(
    url: str,
    token: str,
    *,
    cookie: str | None,
    user_agent: str | None,
) -> dict[str, str]:
    headers = {
        "Accept": "application/grpc-web+proto, application/json",
        "Content-Type": "application/grpc-web+proto",
        "connect-protocol-version": "1",
        "x-grpc-web": "1",
        "Origin": _request_origin(url),
        "Referer": f"{_request_origin(url)}/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "User-Agent": user_agent or _DEFAULT_RESET_USER_AGENT,
    }
    if cookie is not None:
        headers["Cookie"] = cookie
    else:
        headers["Authorization"] = f"Bearer {token}"
        # Keep the bearer fallback compatible with custom gateways and the
        # CLI billing surface. The grok.com web RPC ignores this header.
        headers["x-grok-client-mode"] = "cli"
    return headers


def _open_reset_request(
    request: urllib.request.Request, *, timeout: float
):
    host = urlsplit(request.full_url).hostname
    if host in {"grok.com", "www.grok.com"}:
        return _RESET_IPV4_OPENER.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _grpc_status_error(data: bytes) -> str | None:
    """Return a gRPC-web error message from HTTP/trailer frames, if present."""
    position = 0
    while position + 5 <= len(data):
        flag = data[position]
        length = int.from_bytes(data[position + 1 : position + 5], "big")
        end = position + 5 + length
        if end > len(data):
            break
        if flag & 0x80:
            trailer_text = data[position + 5 : end].decode("ascii", "replace")
            trailers: dict[str, str] = {}
            for line in trailer_text.splitlines():
                name, separator, value = line.partition(":")
                if separator:
                    trailers[name.strip().lower()] = value.strip()
            status = trailers.get("grpc-status")
            if status is not None and status != "0":
                message = unquote(trailers.get("grpc-message", ""))
                return message or f"gRPC status {status}"
        position = end
    return None


def _fetch_grpc_with_curl(
    url: str,
    token: str,
    *,
    timeout: float,
    cookie: str | None,
    user_agent: str | None,
) -> bytes:
    """Fetch the web RPC with cURL when Python TLS is challenged by Cloudflare."""
    curl = shutil.which("curl")
    if curl is None:
        raise FileNotFoundError("curl is required for the grok.com reset RPC")

    args = [
        curl,
        "--silent",
        "--show-error",
        "--ipv4",
        "--request",
        "POST",
        "--data-binary",
        "@-",
        "--write-out",
        "\n%{http_code}",
    ]
    for name, value in _reset_request_headers(
        url,
        token,
        cookie=cookie,
        user_agent=user_agent,
    ).items():
        args.extend(("--header", f"{name}: {value}"))
    args.append(url)

    try:
        result = subprocess.run(
            args,
            input=b"\x00\x00\x00\x00\x00",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("grok reset RPC request timed out") from exc

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail or "curl failed for the grok reset RPC")

    body, separator, status_text = result.stdout.rpartition(b"\n")
    if not separator:
        raise RuntimeError("curl returned no HTTP status for the grok reset RPC")
    try:
        status = int(status_text)
    except ValueError as exc:
        raise RuntimeError("curl returned an invalid HTTP status") from exc
    if status >= 400:
        raise urllib.error.HTTPError(
            url,
            status,
            f"grok reset RPC returned HTTP {status}",
            {},
            None,
        )
    grpc_error = _grpc_status_error(body)
    if grpc_error is not None:
        raise RuntimeError(grpc_error)
    return body


def _fetch_grpc(
    url: str,
    token: str,
    *,
    timeout: float,
    cookie: str | None = None,
    user_agent: str | None = None,
) -> bytes:
    if urlsplit(url).hostname in {"grok.com", "www.grok.com"}:
        try:
            return _fetch_grpc_with_curl(
                url,
                token,
                timeout=timeout,
                cookie=cookie,
                user_agent=user_agent,
            )
        except FileNotFoundError:
            logger.warning("curl is unavailable; trying the standard grok RPC client")

    headers = _reset_request_headers(
        url,
        token,
        cookie=cookie,
        user_agent=user_agent,
    )
    request = urllib.request.Request(
        url,
        data=b"\x00\x00\x00\x00\x00",
        headers=headers,
        method="POST",
    )
    with _open_reset_request(request, timeout=timeout) as response:
        response_headers = getattr(response, "headers", {})
        grpc_status = response_headers.get("grpc-status")
        if grpc_status is not None and grpc_status != "0":
            message = unquote(response_headers.get("grpc-message", ""))
            raise RuntimeError(message or f"gRPC status {grpc_status}")
        body = response.read()
    grpc_error = _grpc_status_error(body)
    if grpc_error is not None:
        raise RuntimeError(grpc_error)
    return body


def _reset_base_urls(base_url: str | None) -> list[str]:
    if base_url is not None:
        return [base_url.rstrip("/")]

    configured = os.environ.get("GROK_RESET_BASE_URL")
    roots: list[str] = []
    if isinstance(configured, str) and configured.strip():
        roots.append(configured.rstrip("/"))
    else:
        roots.append(_DEFAULT_RESET_BASE_URL)
    return roots


def _fetch_resets(
    token: str,
    *,
    base_url: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    cookie: str | None = None,
    user_agent: str | None = None,
) -> list[GrokReset]:
    """Fetch Grok's available one-time usage resets.

    The web client has used both ``ConsumerUiSvc`` service names. Try both so
    the read-only list keeps working as Grok migrates the endpoint. A custom
    ``base_url`` is treated as authoritative for tests and local gateways.

    ``cookie`` may be a complete browser ``Cookie`` header; when omitted, the
    value is read from ``GROK_RESET_COOKIE``. The current grok.com RPC also
    accepts the CLI OAuth bearer, while a browser cookie remains available for
    deployments that require the web session or Cloudflare clearance.
    """
    if cookie is None:
        cookie = _reset_cookie_from_env()
    last_error: Exception | None = None
    got_empty_response = False
    for root in _reset_base_urls(base_url):
        for rpc_base in _RESET_RPC_BASES:
            url = f"{root}{rpc_base}/GetRemainingResets"
            try:
                response = _fetch_grpc(
                    url,
                    token,
                    timeout=timeout,
                    cookie=cookie,
                    user_agent=user_agent,
                )
                resets = _parse_resets_response(response)
                if resets:
                    return resets
                # A service can acknowledge the RPC while returning an empty
                # payload during a backend migration. Keep trying the other
                # service name before deciding that the account has no reset.
                got_empty_response = True
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                OSError,
                TimeoutError,
                RuntimeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                logger.warning("grok reset list check failed for %s: %s", url, exc)
    if last_error is not None:
        raise last_error
    if got_empty_response:
        return []
    return []


def _fetch_quota(
    token: str,
    *,
    base_url: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    reset_cookie: str | None = None,
    reset_user_agent: str | None = None,
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

    status = _parse_billing_payloads(monthly=monthly, credits=credits, user=user)
    try:
        status.resets = _fetch_resets(
            token,
            base_url=base_url,
            timeout=timeout,
            cookie=reset_cookie,
            user_agent=reset_user_agent or _reset_user_agent_from_env(),
        )
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        TimeoutError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        logger.warning("grok reset list check failed (non-blocking): %s", exc)
        status.resets_error = str(exc)
    return status


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
    if fetcher is _fetch_quota:
        _cached_status = fetcher(
            token,
            reset_cookie=_reset_cookie(auth_path),
            reset_user_agent=_reset_user_agent(auth_path),
        )
    else:
        _cached_status = fetcher(token)
    return _cached_status


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
