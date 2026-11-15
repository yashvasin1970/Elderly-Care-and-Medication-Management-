"""Records of doses taken, skipped or snoozed."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from ..engine.models import DoseLog, DoseStatus
from ..timeutil import from_iso, from_iso_or_none, to_iso

#: A dose is identified by its slot and the local date it was due.
DoseKey = tuple[int, date]


def logs_between(
    db: sqlite3.Connection, profile_id: int, first: date, last: date
) -> dict[DoseKey, DoseLog]:
    """All dose logs for a date range (inclusive), keyed by slot and date."""
    rows = db.execute(
        "SELECT * FROM dose_logs WHERE profile_id = ? AND dose_date BETWEEN ? AND ?",
        (profile_id, first.isoformat(), last.isoformat()),
    )
    return {(row["schedule_id"], date.fromisoformat(row["dose_date"])): _log(row) for row in rows}


def get_log(
    db: sqlite3.Connection, profile_id: int, slot_id: int, dose_date: date
) -> DoseLog | None:
    row = db.execute(
        "SELECT * FROM dose_logs WHERE profile_id = ? AND schedule_id = ? AND dose_date = ?",
        (profile_id, slot_id, dose_date.isoformat()),
    ).fetchone()
    return _log(row) if row else None


def save_log(
    db: sqlite3.Connection,
    *,
    profile_id: int,
    slot_id: int,
    dose_date: date,
    status: DoseStatus,
    acted_at: datetime,
    acted_by: int | None,
    snoozed_until: datetime | None = None,
) -> None:
    """Record what happened to a dose, replacing any earlier record of it."""
    db.execute(
        "INSERT INTO dose_logs (profile_id, schedule_id, dose_date, status, snoozed_until,"
        " acted_at, acted_by) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (schedule_id, dose_date) DO UPDATE SET status = excluded.status,"
        " snoozed_until = excluded.snoozed_until, acted_at = excluded.acted_at,"
        " acted_by = excluded.acted_by",
        (
            profile_id,
            slot_id,
            dose_date.isoformat(),
            status.value,
            to_iso(snoozed_until) if snoozed_until else None,
            to_iso(acted_at),
            acted_by,
        ),
    )


def delete_log(db: sqlite3.Connection, profile_id: int, slot_id: int, dose_date: date) -> None:
    db.execute(
        "DELETE FROM dose_logs WHERE profile_id = ? AND schedule_id = ? AND dose_date = ?",
        (profile_id, slot_id, dose_date.isoformat()),
    )


def last_taken_at(db: sqlite3.Connection, profile_id: int) -> datetime | None:
    """When a dose was most recently confirmed as taken."""
    row = db.execute(
        "SELECT MAX(acted_at) FROM dose_logs WHERE profile_id = ? AND status = 'taken'",
        (profile_id,),
    ).fetchone()
    return from_iso_or_none(row[0])


def _log(row: sqlite3.Row) -> DoseLog:
    return DoseLog(
        status=DoseStatus(row["status"]),
        acted_at=from_iso(row["acted_at"]),
        snoozed_until=from_iso_or_none(row["snoozed_until"]),
        acted_by=row["acted_by"],
    )
