"""Normalized usage records for supported coding-agent providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from typing import Any, TypeAlias

from quse.claude_quota import check_claude_quota
from quse.codex_quota import check_codex_quota
from quse.copilot_quota import check_copilot_quota
from quse.grok_quota import check_grok_quota
from quse.zai_quota import check_zai_quota


class UnknownProviderError(ValueError):
    """Raised when a provider name is not supported."""


def usage_provider_error_message(name: str) -> str:
    valid_names = ", ".join(USAGE_PROVIDER_CHOICES)
    return f"Unknown provider '{name}'. Valid provider names: {valid_names}."


def _window_record(window: Any) -> dict[str, Any]:
    if window is None:
        return {"percent_remaining": None, "reset_at": None, "window": None}
    remaining = window.percent_remaining
    if remaining is None:
        percent = None
    else:
        percent = round(float(remaining), 2)
    return {
        "percent_remaining": percent,
        # A real ``datetime`` (or ``None``) — the JSON boundary serializes it to
        # ISO-8601 UTC; the human formatter renders it directly.
        "reset_at": window.reset_at,
        "window": window.window,
    }


def _format_reset_at(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone().strftime("%d-%m-%Y %H:%M (%Z)")


def _format_relative(value: datetime, now: datetime) -> str:
    """Render a reset time as a compact 'in Xd Yh' style countdown.

    ``now`` and ``value`` are compared in UTC; the result is independent of the
    machine's local timezone so output is stable. Days and hours are the only
    meaningful units for these (hours-to-weeks) quota windows.
    """
    delta = value - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= -60:
        return "overdue"
    if total_seconds <= 0:
        return "now"
    minutes, _ = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if not days and not hours:
        parts.append(f"{minutes}m")
    return "in " + " ".join(parts)


def _format_window_hours(value: Any) -> str | None:
    if isinstance(value, int):
        return f"rolling {value}h"
    return None


def _zai_rolling_window(record: dict[str, Any], term: str) -> str | None:
    if record["provider"] != "zai":
        return None
    windows = record["details"].get("windows")
    if not isinstance(windows, dict):
        return None
    window_key = "weekly"
    if term == "short_term":
        window_key = "five_hour"
    window = windows.get(window_key)
    if not isinstance(window, dict):
        return None
    if window_key == "weekly" and window.get("window_hours") is None:
        return "weekly"
    return _format_window_hours(window.get("window_hours"))


def _format_reset_or_window(
    record: dict[str, Any], term: str, *, now: datetime | None = None
) -> str:
    window = record[term]
    reset_at = _format_reset_at(window["reset_at"])
    if reset_at != "unknown":
        if now is not None:
            current = now
        else:
            current = datetime.now(tz=window["reset_at"].tzinfo)
        return f"{reset_at} / {_format_relative(window['reset_at'], current)}"
    rolling_window = _zai_rolling_window(record, term)
    if rolling_window is not None:
        return rolling_window
    return reset_at


def _format_percent(value: float | int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value}%"


def _format_grok_subscription_lines(
    record: dict[str, Any], *, header: bool = True
) -> list[str]:
    if record["provider"] != "grok":
        return []
    details = record.get("details")
    if not isinstance(details, dict):
        return []
    subscription = details.get("subscription")
    if not isinstance(subscription, str) or not subscription:
        return []
    indent, _field_indent = _usage_indents(header)
    return [f"{indent}subscription: {subscription}"]


def _format_codex_reset_credit_lines(
    record: dict[str, Any], *, header: bool = True, now: datetime | None = None
) -> list[str]:
    if record["provider"] != "codex":
        return []
    details = record.get("details")
    if not isinstance(details, dict):
        return []
    credits = details.get("reset_credits")
    if not isinstance(credits, list) or not credits:
        return []
    indent, field_indent = _usage_indents(header)
    return [
        f"{indent}reset_credits:",
        *[
            f"{field_indent}{_format_codex_reset_credit_body(credit, now=now)}"
            for credit in credits
            if isinstance(credit, dict)
        ],
    ]


def _format_grok_reset_lines(
    record: dict[str, Any], *, header: bool = True, now: datetime | None = None
) -> list[str]:
    if record["provider"] != "grok":
        return []
    details = record.get("details")
    if not isinstance(details, dict):
        return []
    resets = details.get("resets")
    if not isinstance(resets, list) or not resets:
        return []
    indent, field_indent = _usage_indents(header)
    return [
        f"{indent}resets:",
        *[
            f"{field_indent}{_format_grok_reset_body(reset, now=now)}"
            for reset in resets
            if isinstance(reset, dict)
        ],
    ]


def _usage_indents(header: bool) -> tuple[str, str]:
    if header:
        return "    ", "        "
    return "", "    "


def _format_codex_reset_credit_body(
    credit: dict[str, Any], *, now: datetime | None = None
) -> str:
    expires_at = credit.get("expires_at")
    formatted = _format_reset_at(expires_at)
    if formatted == "unknown":
        return "expires: unknown"
    if now is not None:
        current = now
    else:
        current = datetime.now(tz=expires_at.tzinfo)
    return f"expires: {formatted} / {_format_relative(expires_at, current)}"


def _format_grok_reset_body(
    reset: dict[str, Any], *, now: datetime | None = None
) -> str:
    expires_at = reset.get("expires_at")
    if expires_at is None:
        expires_at = reset.get("validity_end")
    formatted = _format_reset_at(expires_at)
    if formatted == "unknown":
        return "expires: unknown"
    if now is not None:
        current = now
    else:
        current = datetime.now(tz=expires_at.tzinfo)
    return f"expires: {formatted} / {_format_relative(expires_at, current)}"


def usage_window_record(
    *,
    provider: str,
    status: str,
    short_term: Any,
    long_term: Any,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": status,
        "short_term": _window_record(short_term),
        "long_term": _window_record(long_term),
        "error": error,
        "details": details or {},
    }


class UsageProvider(ABC):
    name: str
    supported: bool = True

    def normalize(self) -> dict[str, Any]:
        if not self.supported:
            return usage_window_record(
                provider=self.name,
                status="unsupported",
                short_term=None,
                long_term=None,
                error="unsupported",
            )

        status_obj = self.check_status()
        return usage_window_record(
            provider=self.name,
            status=self.status_label(status_obj),
            short_term=self.short_term_window(status_obj),
            long_term=self.long_term_window(status_obj),
            error=status_obj.error,
            details=self.details(status_obj),
        )

    @abstractmethod
    def check_status(self) -> Any:
        raise NotImplementedError

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {"limit_reached": status_obj.limit_reached}

    def status_label(self, status_obj: Any) -> str:
        if status_obj.error:
            return "error"
        return "ok"

    def short_term_window(self, status_obj: Any) -> Any:
        if status_obj.error:
            return None
        return status_obj.short_term

    def long_term_window(self, status_obj: Any) -> Any:
        if status_obj.error:
            return None
        return status_obj.long_term


class CodexUsageProvider(UsageProvider):
    name = "codex"

    def check_status(self) -> Any:
        return check_codex_quota()

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "limit_reached": status_obj.limit_reached,
            "reset_credits": [asdict(credit) for credit in status_obj.reset_credits],
            "reset_credits_available": len(status_obj.available_reset_credits),
            "reset_credits_error": status_obj.reset_credits_error,
            "windows": {
                "primary_window": asdict(status_obj.primary_window),
                "secondary_window": asdict(status_obj.secondary_window),
            },
        }


class ClaudeUsageProvider(UsageProvider):
    name = "claude"

    def check_status(self) -> Any:
        return check_claude_quota()

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "limit_reached": status_obj.limit_reached,
            "subscription": status_obj.subscription,
            "windows": {
                "five_hour": asdict(status_obj.five_hour),
                "seven_day": asdict(status_obj.seven_day),
            },
        }


class CopilotUsageProvider(UsageProvider):
    name = "copilot"

    def check_status(self) -> Any:
        return check_copilot_quota()

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "premium_percent_remaining": status_obj.premium_percent_remaining,
            "limit_reached": status_obj.limit_reached,
            "premium_remaining": status_obj.premium_remaining,
            "premium_entitlement": status_obj.premium_entitlement,
        }


class ZaiUsageProvider(UsageProvider):
    name = "zai"

    def check_status(self) -> Any:
        return check_zai_quota()

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "limit_reached": status_obj.limit_reached,
            "max_used_percent": status_obj.max_used_percent,
            "windows": {
                "five_hour": asdict(status_obj.five_hour),
                "weekly": asdict(status_obj.weekly),
                "monthly_web_search": asdict(status_obj.monthly_web_search),
            },
        }


class GrokUsageProvider(UsageProvider):
    name = "grok"

    def check_status(self) -> Any:
        return check_grok_quota()

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "limit_reached": status_obj.limit_reached,
            "subscription": status_obj.subscription,
            "has_grok_code_access": status_obj.has_grok_code_access,
            "is_unified_billing_user": status_obj.is_unified_billing_user,
            "prepaid_balance": status_obj.prepaid_balance,
            "on_demand_cap": status_obj.on_demand_cap,
            "on_demand_used": status_obj.on_demand_used,
            "product_usage": status_obj.product_usage,
            "resets": [asdict(reset) for reset in status_obj.resets],
            "resets_available": len(status_obj.available_resets),
            "resets_error": status_obj.resets_error,
            "windows": {
                "weekly": asdict(status_obj.weekly),
                "monthly": asdict(status_obj.monthly),
            },
        }


class GeminiUsageProvider(UsageProvider):
    name = "gemini"
    supported = False

    def check_status(self) -> Any:
        raise NotImplementedError


UsageProviderClass: TypeAlias = type[UsageProvider]


USAGE_PROVIDER_CLASSES: tuple[UsageProviderClass, ...] = (
    CodexUsageProvider,
    ClaudeUsageProvider,
    ZaiUsageProvider,
    CopilotUsageProvider,
    GrokUsageProvider,
    GeminiUsageProvider,
)
USAGE_PROVIDER_ALIASES = {
    "grok-build": "grok",
}
USAGE_PROVIDER_CHOICES = tuple(
    [provider.name for provider in USAGE_PROVIDER_CLASSES]
    + [alias for alias in USAGE_PROVIDER_ALIASES if alias not in {
        provider.name for provider in USAGE_PROVIDER_CLASSES
    }]
)
SUPPORTED_USAGE_PROVIDERS = tuple(
    provider.name for provider in USAGE_PROVIDER_CLASSES if provider.supported
)


def _canonical_provider_name(name: str) -> str:
    aliased = USAGE_PROVIDER_ALIASES.get(name)
    if aliased is not None:
        return aliased
    return name


def usage_provider_for(name: str) -> UsageProvider:
    canonical = _canonical_provider_name(name)
    for provider_class in USAGE_PROVIDER_CLASSES:
        if provider_class.name == canonical:
            return provider_class()
    raise UnknownProviderError(usage_provider_error_message(name))


def normalize_usage_provider(provider: str) -> dict[str, Any]:
    return usage_provider_for(provider).normalize()


def format_usage_line(
    record: dict[str, Any], *, header: bool = True, now: datetime | None = None
) -> str:
    indent, field_indent = _usage_indents(header)
    lines: list[str] = []
    if header:
        lines.append(f"{record['provider']}:")
    lines.extend(_format_grok_subscription_lines(record, header=header))
    for term in ("short_term", "long_term"):
        window = record[term]
        # Keep the normalized JSON shape stable, but do not print a
        # nonexistent human-facing row when a provider omits a window.
        if record["status"] == "ok" and all(
            window[key] is None
            for key in ("percent_remaining", "reset_at", "window")
        ):
            continue
        usage = _format_percent(window["percent_remaining"])
        lines.extend(
            [
                f"{indent}{term}:",
                f"{field_indent}remaining: {usage}",
                f"{field_indent}reset: {_format_reset_or_window(record, term, now=now)}",
            ]
        )
    lines.extend(_format_codex_reset_credit_lines(record, header=header, now=now))
    lines.extend(_format_grok_reset_lines(record, header=header, now=now))
    if record["error"]:
        lines.append(f"{indent}error: {record['error']}")
    return "\n".join(lines)


def selected_providers(provider: str | None) -> list[str]:
    if provider is None:
        return list(SUPPORTED_USAGE_PROVIDERS)
    if provider not in USAGE_PROVIDER_CHOICES:
        raise UnknownProviderError(usage_provider_error_message(provider))
    return [_canonical_provider_name(provider)]


def collect_usage(provider: str | None = None) -> list[dict[str, Any]]:
    providers = selected_providers(provider)
    if provider is not None:
        return [normalize_usage_provider(providers[0])]
    with ThreadPoolExecutor(max_workers=len(providers)) as executor:
        return list(executor.map(normalize_usage_provider, providers))
