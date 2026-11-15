"""Medicines, their daily time slots, and proposed changes to them."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime

from ..engine.models import Criticality, Medication, Slot
from ..timeutil import from_iso, from_iso_or_none, parse_clock, to_iso


@dataclass(frozen=True, slots=True)
class SlotInput:
    """A time of day for a dose, as entered on the medicine form."""

    anchor: str
    offset_minutes: int = 0
    fixed_time: str | None = None  # "HH:MM", only when anchor is "fixed"


@dataclass(frozen=True, slots=True)
class MedicationInput:
    """Everything needed to create or update a medicine.

    This is also the payload stored with a change request, so a proposal
    carries exactly what would be saved if the elder approves it.
    """

    name: str
    dose: str
    criticality: str
    instructions: str
    missed_dose_rule: str
    pill_color: str
    pill_shape: str
    stock_count: int | None
    units_per_dose: int
    start_date: str
    end_date: str | None
    days_mask: str
    slots: tuple[SlotInput, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> MedicationInput:
        data = json.loads(raw)
        data["slots"] = tuple(SlotInput(**slot) for slot in data["slots"])
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    """A proposed change to the medicine list, awaiting the elder's decision."""

    id: int
    medication_id: int | None
    action: str
    payload: MedicationInput | None
    reason: str
    status: str
    proposed_by: int | None
    created_at: datetime
    decided_at: datetime | None


# --- reading --------------------------------------------------------------


def list_medications(
    db: sqlite3.Connection, profile_id: int, *, include_archived: bool = False
) -> list[Medication]:
    sql = "SELECT * FROM medications WHERE profile_id = ?"
    if not include_archived:
        sql += " AND active = 1"
    rows = db.execute(sql + " ORDER BY name COLLATE NOCASE", (profile_id,))
    return [_medication(row) for row in rows]


def get_medication(
    db: sqlite3.Connection, profile_id: int, medication_id: int
) -> Medication | None:
    row = db.execute(
        "SELECT * FROM medications WHERE id = ? AND profile_id = ?", (medication_id, profile_id)
    ).fetchone()
    return _medication(row) if row else None


def list_slots(db: sqlite3.Connection, profile_id: int) -> list[Slot]:
    """Every slot of the profile, including retired ones (needed for history)."""
    rows = db.execute(
        "SELECT s.* FROM schedules s JOIN medications m ON m.id = s.medication_id"
        " WHERE m.profile_id = ? ORDER BY s.id",
        (profile_id,),
    )
    return [_slot(row) for row in rows]


def get_input(
    db: sqlite3.Connection, profile_id: int, medication_id: int
) -> MedicationInput | None:
    """Load a medicine in the same shape the form submits, for editing or diffing."""
    row = db.execute(
        "SELECT * FROM medications WHERE id = ? AND profile_id = ?", (medication_id, profile_id)
    ).fetchone()
    if row is None:
        return None
    slot_rows = db.execute(
        "SELECT * FROM schedules WHERE medication_id = ? AND retired_at IS NULL ORDER BY id",
        (medication_id,),
    ).fetchall()
    return MedicationInput(
        name=row["name"],
        dose=row["dose"],
        criticality=row["criticality"],
        instructions=row["instructions"],
        missed_dose_rule=row["missed_dose_rule"],
        pill_color=row["pill_color"],
        pill_shape=row["pill_shape"],
        stock_count=row["stock_count"],
        units_per_dose=row["units_per_dose"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        days_mask=slot_rows[0]["days_mask"] if slot_rows else "1111111",
        slots=tuple(
            SlotInput(slot["anchor"], slot["offset_minutes"], slot["fixed_time"])
            for slot in slot_rows
        ),
    )


# --- writing --------------------------------------------------------------


def create_medication(
    db: sqlite3.Connection, profile_id: int, data: MedicationInput, now: datetime
) -> int:
    stamp = to_iso(now)
    cursor = db.execute(
        "INSERT INTO medications (profile_id, name, dose, instructions, missed_dose_rule,"
        " criticality, pill_color, pill_shape, stock_count, units_per_dose, start_date, end_date,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (profile_id, *_medication_values(data), stamp, stamp),
    )
    medication_id = int(cursor.lastrowid or 0)
    _sync_slots(db, medication_id, data, now)
    return medication_id


def update_medication(
    db: sqlite3.Connection,
    profile_id: int,
    medication_id: int,
    data: MedicationInput,
    now: datetime,
) -> None:
    db.execute(
        "UPDATE medications SET name = ?, dose = ?, instructions = ?, missed_dose_rule = ?,"
        " criticality = ?, pill_color = ?, pill_shape = ?, stock_count = ?, units_per_dose = ?,"
        " start_date = ?, end_date = ?, updated_at = ? WHERE id = ? AND profile_id = ?",
        (*_medication_values(data), to_iso(now), medication_id, profile_id),
    )
    _sync_slots(db, medication_id, data, now)


def archive_medication(
    db: sqlite3.Connection, profile_id: int, medication_id: int, now: datetime
) -> None:
    """Stop a medicine. Its history is kept; future doses simply stop."""
    stamp = to_iso(now)
    db.execute(
        "UPDATE medications SET active = 0, updated_at = ? WHERE id = ? AND profile_id = ?",
        (stamp, medication_id, profile_id),
    )
    db.execute(
        "UPDATE schedules SET retired_at = ? WHERE retired_at IS NULL AND medication_id IN"
        " (SELECT id FROM medications WHERE id = ? AND profile_id = ?)",
        (stamp, medication_id, profile_id),
    )


def adjust_stock(db: sqlite3.Connection, profile_id: int, medication_id: int, delta: int) -> None:
    """Add to or take from the tracked stock, never dropping below zero."""
    db.execute(
        "UPDATE medications SET stock_count = MAX(stock_count + ?, 0)"
        " WHERE id = ? AND profile_id = ? AND stock_count IS NOT NULL",
        (delta, medication_id, profile_id),
    )


def restock(db: sqlite3.Connection, profile_id: int, medication_id: int, amount: int) -> None:
    """Add newly bought stock; this also starts tracking stock if it was not tracked."""
    db.execute(
        "UPDATE medications SET stock_count = COALESCE(stock_count, 0) + ?"
        " WHERE id = ? AND profile_id = ?",
        (amount, medication_id, profile_id),
    )


def set_slot_time(
    db: sqlite3.Connection, profile_id: int, slot_id: int, fixed_time: str, now: datetime
) -> bool:
    """Move a fixed-time slot by retiring it and creating its replacement."""
    row = db.execute(
        "SELECT s.* FROM schedules s JOIN medications m ON m.id = s.medication_id"
        " WHERE s.id = ? AND m.profile_id = ? AND s.retired_at IS NULL AND s.anchor = 'fixed'",
        (slot_id, profile_id),
    ).fetchone()
    if row is None:
        return False
    stamp = to_iso(now)
    db.execute("UPDATE schedules SET retired_at = ? WHERE id = ?", (stamp, slot_id))
    db.execute(
        "INSERT INTO schedules (medication_id, anchor, offset_minutes, fixed_time, days_mask,"
        " created_at) VALUES (?, 'fixed', 0, ?, ?, ?)",
        (row["medication_id"], fixed_time, row["days_mask"], stamp),
    )
    return True


def _medication_values(data: MedicationInput) -> tuple[object, ...]:
    return (
        data.name,
        data.dose,
        data.instructions,
        data.missed_dose_rule,
        data.criticality,
        data.pill_color,
        data.pill_shape,
        data.stock_count,
        data.units_per_dose,
        data.start_date,
        data.end_date,
    )


def _sync_slots(
    db: sqlite3.Connection, medication_id: int, data: MedicationInput, now: datetime
) -> None:
    """Bring the stored slots in line with ``data`` without rewriting history.

    Slots that did not change are left alone, so doses already confirmed today
    stay confirmed. Removed slots are retired and new ones are added.
    """
    wanted = [(s.anchor, s.offset_minutes, s.fixed_time, data.days_mask) for s in data.slots]
    stamp = to_iso(now)
    existing = db.execute(
        "SELECT * FROM schedules WHERE medication_id = ? AND retired_at IS NULL", (medication_id,)
    ).fetchall()
    for row in existing:
        key = (row["anchor"], row["offset_minutes"], row["fixed_time"], row["days_mask"])
        if key in wanted:
            wanted.remove(key)
        else:
            db.execute("UPDATE schedules SET retired_at = ? WHERE id = ?", (stamp, row["id"]))
    for anchor, offset_minutes, fixed_time, days_mask in wanted:
        db.execute(
            "INSERT INTO schedules (medication_id, anchor, offset_minutes, fixed_time, days_mask,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (medication_id, anchor, offset_minutes, fixed_time, days_mask, stamp),
        )


# --- change requests ------------------------------------------------------


def create_request(
    db: sqlite3.Connection,
    *,
    profile_id: int,
    medication_id: int | None,
    action: str,
    payload: MedicationInput | None,
    reason: str,
    proposed_by: int,
    now: datetime,
) -> int:
    cursor = db.execute(
        "INSERT INTO change_requests (profile_id, medication_id, action, payload_json, reason,"
        " proposed_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            profile_id,
            medication_id,
            action,
            payload.to_json() if payload else "",
            reason,
            proposed_by,
            to_iso(now),
        ),
    )
    return int(cursor.lastrowid or 0)


def get_request(db: sqlite3.Connection, profile_id: int, request_id: int) -> ChangeRequest | None:
    row = db.execute(
        "SELECT * FROM change_requests WHERE id = ? AND profile_id = ?", (request_id, profile_id)
    ).fetchone()
    return _request(row) if row else None


def list_requests(
    db: sqlite3.Connection, profile_id: int, *, status: str | None = None, limit: int = 50
) -> list[ChangeRequest]:
    sql = "SELECT * FROM change_requests WHERE profile_id = ?"
    params: list[object] = [profile_id]
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    rows = db.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
    return [_request(row) for row in rows]


def count_pending_requests(db: sqlite3.Connection, profile_id: int) -> int:
    row = db.execute(
        "SELECT COUNT(*) FROM change_requests WHERE profile_id = ? AND status = 'pending'",
        (profile_id,),
    ).fetchone()
    return int(row[0])


def decide_request(
    db: sqlite3.Connection,
    profile_id: int,
    request_id: int,
    *,
    status: str,
    decided_by: int,
    now: datetime,
) -> bool:
    """Record a decision. Returns False if the request was no longer pending."""
    cursor = db.execute(
        "UPDATE change_requests SET status = ?, decided_by = ?, decided_at = ?"
        " WHERE id = ? AND profile_id = ? AND status = 'pending'",
        (status, decided_by, to_iso(now), request_id, profile_id),
    )
    return cursor.rowcount == 1


# --- row mapping ----------------------------------------------------------


def _medication(row: sqlite3.Row) -> Medication:
    return Medication(
        id=row["id"],
        name=row["name"],
        dose=row["dose"],
        criticality=Criticality(row["criticality"]),
        start_date=date.fromisoformat(row["start_date"]),
        end_date=date.fromisoformat(row["end_date"]) if row["end_date"] else None,
        instructions=row["instructions"],
        missed_dose_rule=row["missed_dose_rule"],
        pill_color=row["pill_color"],
        pill_shape=row["pill_shape"],
        stock_count=row["stock_count"],
        units_per_dose=row["units_per_dose"],
        active=bool(row["active"]),
    )


def _slot(row: sqlite3.Row) -> Slot:
    return Slot(
        id=row["id"],
        medication_id=row["medication_id"],
        anchor=row["anchor"],
        created_at=from_iso(row["created_at"]),
        offset_minutes=row["offset_minutes"],
        fixed_time=parse_clock(row["fixed_time"]) if row["fixed_time"] else None,
        days_mask=row["days_mask"],
        retired_at=from_iso_or_none(row["retired_at"]),
    )


def _request(row: sqlite3.Row) -> ChangeRequest:
    return ChangeRequest(
        id=row["id"],
        medication_id=row["medication_id"],
        action=row["action"],
        payload=MedicationInput.from_json(row["payload_json"]) if row["payload_json"] else None,
        reason=row["reason"],
        status=row["status"],
        proposed_by=row["proposed_by"],
        created_at=from_iso(row["created_at"]),
        decided_at=from_iso_or_none(row["decided_at"]),
    )
