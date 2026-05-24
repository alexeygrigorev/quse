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


def usage_window_record(
    *,
    provider: str,
    status: str,
    used: float | int | None,
    limit: float | int | None,
    remaining: float | int | None,
    unit: str | None,
    reset_window: str | None,
    reset_at: str | None,
    block_reason: str | None,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": status,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "unit": unit,
        "reset_window": reset_window,
        "reset_at": reset_at,
        "block_reason": block_reason,
        "error": error,
        "details": details or {},
    }


def _pick_named_window(windows: dict[str, Any]) -> tuple[str, Any]:
    return max(windows.items(), key=lambda item: item[1].used_percent)


def normalize_usage_provider(provider: str) -> dict[str, Any]:
    if provider == "gemini":
        return usage_window_record(
            provider="gemini",
            status="unsupported",
            used=None,
            limit=None,
            remaining=None,
            unit=None,
            reset_window=None,
            reset_at=None,
            block_reason=None,
            error="unsupported",
        )

    if provider == "codex":
        import quse

        status_obj = quse.check_codex_quota()
        block_reason = quse.codex_quota_block_reason()
        windows = {
            "primary_window": status_obj.primary_window,
            "secondary_window": status_obj.secondary_window,
        }
        selected_name, selected_window = _pick_named_window(windows)
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        used = round(selected_window.used_percent, 2)
        limit = 100.0
        remaining = round(max(0.0, limit - used), 2)
        return usage_window_record(
            provider=provider,
            status=status,
            used=None if status_obj.error else used,
            limit=None if status_obj.error else limit,
            remaining=None if status_obj.error else remaining,
            unit="percent",
            reset_window=selected_name,
            reset_at=selected_window.reset_at or status_obj.earliest_reset_at,
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
        windows = {
            "five_hour": status_obj.five_hour,
            "seven_day": status_obj.seven_day,
        }
        selected_name, selected_window = _pick_named_window(windows)
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        used = round(selected_window.used_percent, 2)
        limit = 100.0
        remaining = round(max(0.0, limit - used), 2)
        return usage_window_record(
            provider=provider,
            status=status,
            used=None if status_obj.error else used,
            limit=None if status_obj.error else limit,
            remaining=None if status_obj.error else remaining,
            unit="percent",
            reset_window=selected_name,
            reset_at=selected_window.reset_at,
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
        used = round(status_obj.used_percent, 2)
        limit = status_obj.premium_entitlement
        remaining = status_obj.premium_remaining
        return usage_window_record(
            provider=provider,
            status=status,
            used=None if status_obj.error else used,
            limit=None if status_obj.error else limit,
            remaining=None if status_obj.error else remaining,
            unit="premium_interactions",
            reset_window="monthly",
            reset_at=status_obj.quota_reset_date,
            block_reason=block_reason,
            error=status_obj.error,
            details={
                "premium_percent_remaining": status_obj.premium_percent_remaining,
                "limit_reached": status_obj.limit_reached,
            },
        )

    if provider == "zai":
        import quse

        status_obj = quse.check_zai_quota()
        block_reason = quse.zai_quota_block_reason()
        windows = {
            "api_calls": status_obj.api_calls,
            "tokens": status_obj.tokens,
        }
        selected_name, selected_window = _pick_named_window(windows)
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        used = None
        if selected_window.limit is not None and selected_window.remaining is not None:
            used = selected_window.limit - selected_window.remaining
        return usage_window_record(
            provider=provider,
            status=status,
            used=None if status_obj.error else used,
            limit=None if status_obj.error else selected_window.limit,
            remaining=None if status_obj.error else selected_window.remaining,
            unit=selected_name,
            reset_window=f"{selected_window.window_hours}h" if selected_window.window_hours else selected_name,
            reset_at=None,
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
    fields = [
        record["provider"] + ":",
        f"status={record['status']}",
        f"used={record['used'] if record['used'] is not None else 'unknown'}",
        f"limit={record['limit'] if record['limit'] is not None else 'unknown'}",
        f"remaining={record['remaining'] if record['remaining'] is not None else 'unknown'}",
        f"unit={record['unit'] or 'unknown'}",
        f"reset_window={record['reset_window'] or 'unknown'}",
        f"reset_at={record['reset_at'] or 'unknown'}",
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
