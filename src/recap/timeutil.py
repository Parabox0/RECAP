"""Time helpers.

Every time inside RECAP is an integer number of seconds since the Unix epoch,
in UTC. Public functions accept a wider range of inputs and normalise them.
"""

from __future__ import annotations

import datetime as _dt
import email.utils

HOUR = 3600
DAY = 86_400
_UTC = _dt.timezone.utc

TimeLike = int | float | str | _dt.datetime | _dt.date | None


def parse_time(value: TimeLike) -> int | None:
    """Return ``value`` as epoch seconds (UTC), or ``None`` for empty input.

    Accepts epoch numbers, ``datetime``/``date`` objects (naive values are
    treated as UTC), ISO 8601 / RFC 3339 strings, plain dates and RFC 2822
    dates as used by RSS.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("booleans are not times")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_UTC)
        return int(value.timestamp())
    if isinstance(value, _dt.date):
        return int(_dt.datetime(value.year, value.month, value.day, tzinfo=_UTC).timestamp())
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError) as exc:
            raise ValueError(f"unrecognised time: {text!r}") from exc
        if parsed is None:  # pragma: no cover - older Pythons returned None
            raise ValueError(f"unrecognised time: {text!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return int(parsed.timestamp())


def iso(ts: int | None) -> str | None:
    """Format epoch seconds as ``YYYY-MM-DDTHH:MM:SSZ``."""
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, _UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_str(ts: int) -> str:
    """Format epoch seconds as ``YYYY-MM-DD`` (UTC)."""
    return _dt.datetime.fromtimestamp(ts, _UTC).strftime("%Y-%m-%d")


def hour_bucket(ts: int) -> int:
    """Round down to the start of the hour."""
    return ts - (ts % HOUR)
