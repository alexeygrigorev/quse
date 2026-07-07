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
from quse.zai_quota import check_zai_quota


class UnknownProviderError(ValueError):
    """Raised when a provider name is not supported."""


def usage_provider_error_message(name: str) -> str:
    valid_names = ", ".join(USAGE_PROVIDER_CHOICES)
    return f"Unknown provider '{name}'. Valid provider names: {valid_names}."


def _window_record(window: Any) -> dict[str, Any]:
    if window is None:
        return {"percent_remaining": None, "reset_at": None, "window": None}
    return {
        "percent_remaining": round(float(window.percent_remaining), 2),
        # A real ``datetime`` (or ``None``) — the JSON boundary serializes it to
        # ISO-8601 UTC; the human formatter renders it directly.
        "reset_at": window.reset_at,
        "window": window.window,
    }


def _format_reset_at(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone().strftime("%d-%m-%Y %H:%M (%Z)")


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


def _format_reset_or_window(record: dict[str, Any], term: str) -> str:
    window = record[term]
    reset_at = _format_reset_at(window["reset_at"])
    if reset_at != "unknown":
        return reset_at
    rolling_window = _zai_rolling_window(record, term)
    if rolling_window is not None:
        return rolling_window
    return reset_at


def _format_percent(value: float | int | None) -> str:
    if value is None:
        return "unknown"
    return str(value)


def _format_codex_reset_credit_lines(
    record: dict[str, Any], *, header: bool = True
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
            f"{field_indent}{_format_codex_reset_credit_body(credit)}"
            for credit in credits
            if isinstance(credit, dict)
        ],
    ]


def _usage_indents(header: bool) -> tuple[str, str]:
    if header:
        return "    ", "        "
    return "", "    "


def _format_codex_reset_credit_body(credit: dict[str, Any]) -> str:
    title = credit.get("title") or credit.get("status") or "reset credit"
    title = str(title).split("(", maxsplit=1)[0].strip().lower() or "reset credit"
    expires_at = _format_reset_at(credit.get("expires_at"))
    if expires_at == "unknown":
        return title
    return f"{title} | expires: {expires_at}"


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
    GeminiUsageProvider,
)
USAGE_PROVIDER_CHOICES = tuple(provider.name for provider in USAGE_PROVIDER_CLASSES)
SUPPORTED_USAGE_PROVIDERS = tuple(
    provider.name for provider in USAGE_PROVIDER_CLASSES if provider.supported
)


def usage_provider_for(name: str) -> UsageProvider:
    for provider_class in USAGE_PROVIDER_CLASSES:
        if provider_class.name == name:
            return provider_class()
    raise UnknownProviderError(usage_provider_error_message(name))


def normalize_usage_provider(provider: str) -> dict[str, Any]:
    return usage_provider_for(provider).normalize()


def format_usage_line(record: dict[str, Any], *, header: bool = True) -> str:
    short_term = record["short_term"]
    long_term = record["long_term"]
    short_usage = _format_percent(short_term["percent_remaining"])
    long_usage = _format_percent(long_term["percent_remaining"])
    indent, field_indent = _usage_indents(header)
    lines = [
        f"{indent}status: {record['status']}",
        f"{indent}short_term:",
        f"{field_indent}remaining: {short_usage}%",
        f"{field_indent}reset: {_format_reset_or_window(record, 'short_term')}",
        f"{indent}long_term:",
        f"{field_indent}remaining: {long_usage}%",
        f"{field_indent}reset: {_format_reset_or_window(record, 'long_term')}",
    ]
    if header:
        lines.insert(0, f"{record['provider']}:")
    lines.extend(_format_codex_reset_credit_lines(record, header=header))
    if record["error"]:
        lines.append(f"{indent}error: {record['error']}")
    return "\n".join(lines)


def selected_providers(provider: str | None) -> list[str]:
    if provider is None:
        providers = list(SUPPORTED_USAGE_PROVIDERS)
    else:
        providers = [provider]
    for name in providers:
        if name not in USAGE_PROVIDER_CHOICES:
            raise UnknownProviderError(usage_provider_error_message(name))
    return providers


def collect_usage(provider: str | None = None) -> list[dict[str, Any]]:
    providers = selected_providers(provider)
    if provider is not None:
        return [normalize_usage_provider(providers[0])]
    with ThreadPoolExecutor(max_workers=len(providers)) as executor:
        return list(executor.map(normalize_usage_provider, providers))
