"""Shared quota status models and normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class UsageWindow:
    percent_remaining: float | None = 100.0
    # A real, timezone-aware UTC ``datetime`` (or ``None``). quse is the single
    # source of truth for the reset time: it parses every provider's raw value
    # (epoch, ISO, date-only) into one canonical ``datetime`` here, so downstream
    # consumers never re-parse a string. Serialized to ISO-8601 UTC at the JSON
    # boundary via [reset_at_to_iso].
    reset_at: datetime | None = None
    # Provider-native span label used inside adapters and diagnostics
    # (``5h`` / ``7d`` / ``weekly`` / ``monthly`` / ...). The final display
    # schema uses the canonical map keys instead, so this is not serialized as
    # a separate field. ``None`` for windows with no meaningful fixed span.
    window: str | None = None
    # True only when the provider explicitly identifies this window as rolling.
    # The normalized display contract currently exposes this on the 5h window.
    rolling: bool = False

    @property
    def used_percent(self) -> float:
        if self.percent_remaining is None:
            return 0.0
        return max(0.0, 100.0 - self.percent_remaining)


@dataclass(slots=True)
class UsageStatus:
    limit_reached: bool = False
    short_term: UsageWindow = field(default_factory=UsageWindow)
    long_term: UsageWindow = field(default_factory=UsageWindow)
    checked_at: float = 0.0
    error: str | None = None


def normalize_reset_at(value: object) -> datetime | None:
    """Parse a provider's raw reset value into a canonical UTC ``datetime``.

    Accepts an epoch (int/float or digit string), an ISO-8601 string (with or
    without ``Z``), a date-only ``YYYY-MM-DD`` string, or an existing
    ``datetime``. Returns a timezone-aware UTC ``datetime``, or ``None`` when the
    value is missing / unparseable. This is the single normalization point —
    downstream code holds a real ``datetime``, never a string it must re-parse.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = _epoch_to_datetime(float(value))
        if parsed is None:
            return None
    else:
        normalized = str(value).strip()
        if not normalized:
            return None
        if normalized.isdigit():
            parsed = _epoch_to_datetime(float(normalized))
            if parsed is None:
                return None
        else:
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
    return parsed.astimezone(timezone.utc)


def _epoch_to_datetime(timestamp: float) -> datetime | None:
    """Convert a Unix epoch (seconds OR milliseconds) to a UTC ``datetime``.

    Values whose magnitude exceeds ~1e10 are treated as milliseconds (current
    epoch-seconds are ~1.7e9), matching the millisecond timestamps Codex emits.
    """
    if abs(timestamp) > 10_000_000_000:
        timestamp = timestamp / 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def reset_at_to_iso(value: datetime | None) -> str | None:
    """Serialize a canonical UTC ``datetime`` to ISO-8601 (``...Z``) for JSON."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
