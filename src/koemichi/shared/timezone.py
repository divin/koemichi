"""Convert recording timestamps to the configured local timezone."""

from datetime import datetime
from zoneinfo import ZoneInfo

from .settings import TIMEZONE


def ts_to_local(ts_ms: int) -> datetime:
    """Convert Unix epoch milliseconds to the configured local timezone.

    Parameters
    ----------
    ts_ms : int
        Timestamp in milliseconds since the Unix epoch.

    Returns
    -------
    datetime
        Timezone-aware local datetime.
    """
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=ZoneInfo("UTC")).astimezone(
        TIMEZONE
    )
