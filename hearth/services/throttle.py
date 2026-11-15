"""Attempt counters that lock out repeated guessing.

Counters live in the database rather than in memory, so a lockout survives a
restart and is shared by every worker process.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..db import transaction
from ..timeutil import from_iso, from_iso_or_none, to_iso


@dataclass(frozen=True, slots=True)
class ThrottleRule:
    """Lock a bucket for ``lockout`` after ``max_attempts`` within ``window``."""

    max_attempts: int
    window: timedelta
    lockout: timedelta


_QUARTER_HOUR = timedelta(minutes=15)
_HOUR = timedelta(hours=1)

# Sign-in: strict per visitor, looser per circle so that one person guessing
# cannot easily lock the whole family out.
LOGIN_PER_CIRCLE_AND_ADDRESS = ThrottleRule(5, _QUARTER_HOUR, _QUARTER_HOUR)
LOGIN_PER_CIRCLE = ThrottleRule(25, _QUARTER_HOUR, _QUARTER_HOUR)
LOGIN_PER_ADDRESS = ThrottleRule(20, _QUARTER_HOUR, _QUARTER_HOUR)
NEW_CIRCLES_PER_ADDRESS = ThrottleRule(5, _HOUR, _HOUR)
SMART_ADD_PER_MEMBER = ThrottleRule(20, _HOUR, _HOUR)


def locked_until(db: sqlite3.Connection, bucket: str, now: datetime) -> datetime | None:
    """Return when the bucket unlocks, or ``None`` if it is not locked."""
    row = db.execute("SELECT locked_until FROM throttle WHERE bucket = ?", (bucket,)).fetchone()
    until = from_iso_or_none(row["locked_until"]) if row else None
    return until if until is not None and until > now else None


def record_attempt(db: sqlite3.Connection, bucket: str, rule: ThrottleRule, now: datetime) -> bool:
    """Count one attempt. Returns True if the bucket is now locked."""
    with transaction(db):
        row = db.execute("SELECT * FROM throttle WHERE bucket = ?", (bucket,)).fetchone()
        if row is None or now - from_iso(row["window_start"]) > rule.window:
            attempts, window_start = 1, now
        else:
            attempts, window_start = row["attempts"] + 1, from_iso(row["window_start"])
        locked = attempts >= rule.max_attempts
        db.execute(
            "INSERT INTO throttle (bucket, attempts, window_start, locked_until)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (bucket) DO UPDATE SET attempts = excluded.attempts,"
            " window_start = excluded.window_start, locked_until = excluded.locked_until",
            (
                bucket,
                attempts,
                to_iso(window_start),
                to_iso(now + rule.lockout) if locked else None,
            ),
        )
    return locked


def clear(db: sqlite3.Connection, bucket: str) -> None:
    """Forget a bucket, for example after a successful sign-in."""
    db.execute("DELETE FROM throttle WHERE bucket = ?", (bucket,))
