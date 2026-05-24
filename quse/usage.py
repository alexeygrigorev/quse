"""Normalized usage records for supported coding-agent providers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

USAGE_PROVIDER_CHOICES = ("codex", "claude", "copilot", "zai", "gemini")
SUPPORTED_USAGE_PROVIDERS = ("codex", "claude", "copilot", "zai")


class UnknownProviderError(ValueError):
    """Raised when a provider name is not supported."""


def usage_provider_error_message(name: str) -> str:
    valid_names = ", ".join(USAGE_PROVIDER_CHOICES)
    return f"Unknown provider '{name}'. Valid provider names: {valid_names}."


def _window_record(window: Any) -> dict[str, Any]:
    return {
        "percent_remaining": None
        if window is None
        else round(float(window.percent_remaining), 2),
        "reset_at": None if window is None else window.reset_at,
    }


def usage_window_record(
    *,
    provider: str,
    status: str,
    short_term: Any,
    long_term: Any,
    block_reason: str | None,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": status,
        "short_term": _window_record(short_term),
        "long_term": _window_record(long_term),
        "block_reason": block_reason,
        "error": error,
        "details": details or {},
    }


def normalize_usage_provider(provider: str) -> dict[str, Any]:
    if provider == "gemini":
        return usage_window_record(
            provider="gemini",
            status="unsupported",
            short_term=None,
            long_term=None,
            block_reason=None,
            error="unsupported",
        )

    if provider == "codex":
        import quse

        status_obj = quse.check_codex_quota()
        block_reason = quse.codex_quota_block_reason()
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        return usage_window_record(
            provider=provider,
            status=status,
            short_term=None if status_obj.error else status_obj.short_term,
            long_term=None if status_obj.error else status_obj.long_term,
            block_reason=block_reason,
            error=status_obj.error,
            details={
                "limit_reached": status_obj.limit_reached,
                "windows": {
                    "primary_window": asdict(status_obj.primary_window),
                    "secondary_window": asdict(status_obj.secondary_window),
                },
            },
        )

    if provider == "claude":
        import quse

        status_obj = quse.check_claude_quota()
        block_reason = quse.claude_quota_block_reason()
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        return usage_window_record(
            provider=provider,
            status=status,
            short_term=None if status_obj.error else status_obj.short_term,
            long_term=None if status_obj.error else status_obj.long_term,
            block_reason=block_reason,
            error=status_obj.error,
            details={
                "limit_reached": status_obj.limit_reached,
                "subscription": status_obj.subscription,
                "windows": {
                    "five_hour": asdict(status_obj.five_hour),
                    "seven_day": asdict(status_obj.seven_day),
                },
            },
        )

    if provider == "copilot":
        import quse

        status_obj = quse.check_copilot_quota()
        block_reason = quse.copilot_quota_block_reason()
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        return usage_window_record(
            provider=provider,
            status=status,
            short_term=None if status_obj.error else status_obj.short_term,
            long_term=None if status_obj.error else status_obj.long_term,
            block_reason=block_reason,
            error=status_obj.error,
            details={
                "premium_percent_remaining": status_obj.premium_percent_remaining,
                "limit_reached": status_obj.limit_reached,
                "premium_remaining": status_obj.premium_remaining,
                "premium_entitlement": status_obj.premium_entitlement,
            },
        )

    if provider == "zai":
        import quse

        status_obj = quse.check_zai_quota()
        block_reason = quse.zai_quota_block_reason()
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        return usage_window_record(
            provider=provider,
            status=status,
            short_term=None if status_obj.error else status_obj.short_term,
            long_term=None if status_obj.error else status_obj.long_term,
            block_reason=block_reason,
            error=status_obj.error,
            details={
                "limit_reached": status_obj.limit_reached,
                "max_used_percent": status_obj.max_used_percent,
                "windows": {
                    "api_calls": asdict(status_obj.api_calls),
                    "tokens": asdict(status_obj.tokens),
                },
            },
        )

    raise UnknownProviderError(usage_provider_error_message(provider))


def format_usage_line(record: dict[str, Any]) -> str:
    short_term = record["short_term"]
    long_term = record["long_term"]
    fields = [
        record["provider"] + ":",
        f"status={record['status']}",
        f"short_term={short_term['percent_remaining'] if short_term['percent_remaining'] is not None else 'unknown'}%",
        f"short_reset={short_term['reset_at'] or 'unknown'}",
        f"long_term={long_term['percent_remaining'] if long_term['percent_remaining'] is not None else 'unknown'}%",
        f"long_reset={long_term['reset_at'] or 'unknown'}",
    ]
    if record["block_reason"]:
        fields.append(f"block_reason={record['block_reason']}")
    if record["error"]:
        fields.append(f"error={record['error']}")
    return " ".join(fields)


def selected_providers(provider: str | None) -> list[str]:
    providers = [provider] if provider is not None else list(SUPPORTED_USAGE_PROVIDERS)
    for name in providers:
        if name not in USAGE_PROVIDER_CHOICES:
            raise UnknownProviderError(usage_provider_error_message(name))
    return providers


def collect_usage(provider: str | None = None) -> list[dict[str, Any]]:
    return [normalize_usage_provider(name) for name in selected_providers(provider)]
