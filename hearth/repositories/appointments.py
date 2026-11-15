"""Doctor appointments."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..timeutil import from_iso, to_iso


@dataclass(frozen=True, slots=True)
class Appointment:
    id: int
    title: str
    doctor: str
    location: str
    starts_at: datetime
    notes: str
    cancelled: bool


@dataclass(frozen=True, slots=True)
class AppointmentInput:
    """An appointment as entered on the form."""

    title: str
    doctor: str
    location: str
    starts_at: datetime
    notes: str


def list_between(
    db: sqlite3.Connection, profile_id: int, start: datetime, end: datetime
) -> list[Appointment]:
    """Appointments (not cancelled) starting in ``[start, end)``, earliest first."""
    rows = db.execute(
        "SELECT * FROM appointments WHERE profile_id = ? AND cancelled = 0"
        " AND starts_at >= ? AND starts_at < ? ORDER BY starts_at",
        (profile_id, to_iso(start), to_iso(end)),
    )
    return [_appointment(row) for row in rows]


def list_upcoming(
    db: sqlite3.Connection, profile_id: int, now: datetime, limit: int = 20
) -> list[Appointment]:
    rows = db.execute(
        "SELECT * FROM appointments WHERE profile_id = ? AND cancelled = 0 AND starts_at >= ?"
        " ORDER BY starts_at LIMIT ?",
        (profile_id, to_iso(now), limit),
    )
    return [_appointment(row) for row in rows]


def list_past(
    db: sqlite3.Connection, profile_id: int, now: datetime, limit: int = 10
) -> list[Appointment]:
    rows = db.execute(
        "SELECT * FROM appointments WHERE profile_id = ? AND cancelled = 0 AND starts_at < ?"
        " ORDER BY starts_at DESC LIMIT ?",
        (profile_id, to_iso(now), limit),
    )
    return [_appointment(row) for row in rows]


def previous_before(
    db: sqlite3.Connection, profile_id: int, moment: datetime
) -> Appointment | None:
    """The most recent appointment before ``moment``, if any."""
    found = list_past(db, profile_id, moment, limit=1)
    return found[0] if found else None


def get_appointment(
    db: sqlite3.Connection, profile_id: int, appointment_id: int
) -> Appointment | None:
    row = db.execute(
        "SELECT * FROM appointments WHERE id = ? AND profile_id = ?", (appointment_id, profile_id)
    ).fetchone()
    return _appointment(row) if row else None


def create_appointment(
    db: sqlite3.Connection,
    profile_id: int,
    data: AppointmentInput,
    *,
    created_by: int,
    now: datetime,
) -> int:
    cursor = db.execute(
        "INSERT INTO appointments (profile_id, title, doctor, location, starts_at, notes,"
        " created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            profile_id,
            data.title,
            data.doctor,
            data.location,
            to_iso(data.starts_at),
            data.notes,
            created_by,
            to_iso(now),
        ),
    )
    return int(cursor.lastrowid or 0)


def update_appointment(
    db: sqlite3.Connection, profile_id: int, appointment_id: int, data: AppointmentInput
) -> None:
    db.execute(
        "UPDATE appointments SET title = ?, doctor = ?, location = ?, starts_at = ?, notes = ?"
        " WHERE id = ? AND profile_id = ?",
        (
            data.title,
            data.doctor,
            data.location,
            to_iso(data.starts_at),
            data.notes,
            appointment_id,
            profile_id,
        ),
    )


def cancel_appointment(db: sqlite3.Connection, profile_id: int, appointment_id: int) -> None:
    db.execute(
        "UPDATE appointments SET cancelled = 1 WHERE id = ? AND profile_id = ?",
        (appointment_id, profile_id),
    )


def _appointment(row: sqlite3.Row) -> Appointment:
    return Appointment(
        id=row["id"],
        title=row["title"],
        doctor=row["doctor"],
        location=row["location"],
        starts_at=from_iso(row["starts_at"]),
        notes=row["notes"],
        cancelled=bool(row["cancelled"]),
    )
