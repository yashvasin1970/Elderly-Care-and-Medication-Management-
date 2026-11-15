"""Small helpers for times, dates and timezones.

The database stores moments as UTC ISO-8601 strings. Everything shown to a
person is converted to the elder's own timezone first.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

_REGIONS = frozenset(
    {
        "Africa",
        "America",
        "Antarctica",
        "Asia",
        "Atlantic",
        "Australia",
        "Europe",
        "Indian",
        "Pacific",
    }
)


def utc_now() -> datetime:
    """The current moment, timezone-aware, in UTC."""
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    """Serialise an aware datetime as a UTC ISO-8601 string."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    """Parse a stored timestamp; naive values are taken to be UTC."""
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def from_iso_or_none(value: str | None) -> datetime | None:
    return from_iso(value) if value else None


def parse_clock(value: str) -> time:
    """Parse "HH:MM" into a time; raises ``ValueError`` if it is malformed."""
    return datetime.strptime(value.strip(), "%H:%M").time()


def format_clock(value: time) -> str:
    return value.strftime("%H:%M")


def parse_date(value: str) -> date:
    """Parse "YYYY-MM-DD"; raises ``ValueError`` if it is malformed."""
    return date.fromisoformat(value.strip())


def friendly_clock(value: time | datetime) -> str:
    """Format a time the way people say it: "8:15 AM"."""
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


@lru_cache(maxsize=1)
def timezone_names() -> tuple[str, ...]:
    """City-based timezone names for pick lists, plus UTC."""
    names = {name for name in available_timezones() if _is_city_zone(name)}
    return ("UTC", *sorted(names))


def _is_city_zone(name: str) -> bool:
    region = name.split("/", 1)[0]
    return "/" in name and region in _REGIONS


def is_valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def get_zone(name: str) -> ZoneInfo:
    """Look up a timezone by name, falling back to UTC for unknown names."""
    return ZoneInfo(name) if is_valid_zone(name) else ZoneInfo("UTC")
