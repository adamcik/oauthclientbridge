from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return aware UTC wall-clock time.

    Use for timestamps that may be stored, serialized, or compared with
    external datetimes. Do not use for measuring elapsed time; use
    time.monotonic() for durations.
    """

    return datetime.now(UTC)
