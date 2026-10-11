"""Shared local-time helpers for transcript workflows."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


def local_recording_datetime(recorded_at_ms: int, timezone: ZoneInfo) -> datetime:
    """Convert the upstream recording timestamp to its configured local time."""
    try:
        recorded_at = datetime.fromtimestamp(recorded_at_ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(
            "recorded_at_ms is outside the supported timestamp range"
        ) from exc
    return recorded_at.astimezone(timezone)


def local_recording_date(recorded_at_ms: int, timezone: ZoneInfo) -> date:
    """Convert the upstream recording timestamp to its local calendar date."""
    return local_recording_datetime(recorded_at_ms, timezone).date()
