"""Shared quota status models and normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class UsageWindow:
    percent_remaining: float = 100.0
    reset_at: str | None = None

    @property
    def used_percent(self) -> float:
        return max(0.0, 100.0 - self.percent_remaining)


@dataclass(slots=True)
class UsageStatus:
    limit_reached: bool = False
    short_term: UsageWindow = field(default_factory=UsageWindow)
    long_term: UsageWindow = field(default_factory=UsageWindow)
    checked_at: float = 0.0
    error: str | None = None


def preferred_reset_at(
    status: UsageStatus,
    *,
    include_short_term_fallback: bool = False,
) -> str | None:
    if status.long_term.reset_at:
        return status.long_term.reset_at
    if include_short_term_fallback:
        return status.short_term.reset_at
    return None


def usage_limit_block_reason(engine_name: str, status: UsageStatus) -> str | None:
    if status.error:
        return None
    if engine_name == "codex":
        from quse.codex_quota import codex_quota_block_reason

        return codex_quota_block_reason()
    if engine_name == "claude":
        from quse.claude_quota import claude_quota_block_reason

        return claude_quota_block_reason()
    if engine_name == "copilot":
        from quse.copilot_quota import copilot_quota_block_reason

        return copilot_quota_block_reason()
    if engine_name in {"goz", "opencode"}:
        from quse.zai_quota import zai_quota_block_reason

        return zai_quota_block_reason()
    if not status.limit_reached:
        return None
    reset_at = preferred_reset_at(status, include_short_term_fallback=True)
    reset_suffix = f", resets {reset_at}" if reset_at else ""
    return f"{engine_name} usage limit reached{reset_suffix}"


def normalize_reset_at(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = str(value).strip()
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(normalized, "%Y-%m-%d")
            except ValueError:
                return None
            parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
