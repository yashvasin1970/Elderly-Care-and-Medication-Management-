"""Small builders for engine objects, so tests state only what matters."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from hearth.engine.models import (
    DEFAULT_ROUTINE,
    Criticality,
    DoseLog,
    DoseStatus,
    Medication,
    Occurrence,
    Slot,
)

TZ = ZoneInfo("Asia/Kolkata")
LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)
ROUTINE = dict(DEFAULT_ROUTINE)


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A moment in the test profile's timezone."""
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def make_medication(
    med_id: int = 1,
    name: str = "Metformin",
    criticality: Criticality = Criticality.IMPORTANT,
    **overrides: object,
) -> Medication:
    values: dict[str, object] = {
        "id": med_id,
        "name": name,
        "dose": "500 mg",
        "criticality": criticality,
        "start_date": date(2026, 1, 1),
    }
    values.update(overrides)
    return Medication(**values)  # type: ignore[arg-type]


def make_slot(
    slot_id: int = 1,
    medication_id: int = 1,
    anchor: str = "breakfast",
    **overrides: object,
) -> Slot:
    values: dict[str, object] = {
        "id": slot_id,
        "medication_id": medication_id,
        "anchor": anchor,
        "created_at": LONG_AGO,
    }
    values.update(overrides)
    return Slot(**values)  # type: ignore[arg-type]


def make_occurrence(
    scheduled_at: datetime,
    criticality: Criticality = Criticality.IMPORTANT,
) -> Occurrence:
    medication = make_medication(criticality=criticality)
    slot = make_slot(anchor="fixed", fixed_time=time(scheduled_at.hour, scheduled_at.minute))
    return Occurrence(slot, medication, scheduled_at.date(), scheduled_at)


def taken(at: datetime) -> DoseLog:
    return DoseLog(DoseStatus.TAKEN, at)


def skipped(at: datetime) -> DoseLog:
    return DoseLog(DoseStatus.SKIPPED, at)


def snoozed(at: datetime, until: datetime) -> DoseLog:
    return DoseLog(DoseStatus.SNOOZED, at, snoozed_until=until)
