"""Turn medication slots into the concrete doses due on a given day."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, tzinfo

from .models import FIXED, Medication, Occurrence, Routine, Slot

_LAST_MINUTE_OF_DAY = 23 * 60 + 59

_ANCHOR_LABELS = {
    "wake": "waking up",
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "bedtime": "bedtime",
}

_ON_ANCHOR_LABELS = {
    "wake": "On waking up",
    "breakfast": "With breakfast",
    "lunch": "With lunch",
    "dinner": "With dinner",
    "bedtime": "At bedtime",
}


def slot_time(slot: Slot, routine: Routine) -> time:
    """Return the clock time a slot falls on under the given routine.

    Anchored slots move with the routine. The result is clamped to the same
    day so a late bedtime plus an offset can never spill past midnight.
    """
    if slot.anchor == FIXED:
        if slot.fixed_time is None:
            raise ValueError("A fixed slot needs a fixed_time.")
        return slot.fixed_time
    base = routine[slot.anchor]
    minutes = base.hour * 60 + base.minute + slot.offset_minutes
    minutes = max(0, min(minutes, _LAST_MINUTE_OF_DAY))
    return time(minutes // 60, minutes % 60)


def describe_slot(slot: Slot) -> str:
    """Describe a slot in everyday words, e.g. "After breakfast"."""
    if slot.anchor == FIXED:
        return "At a set time"
    if slot.offset_minutes == 0:
        return _ON_ANCHOR_LABELS[slot.anchor]
    label = _ANCHOR_LABELS[slot.anchor]
    amount = _describe_minutes(abs(slot.offset_minutes))
    direction = "before" if slot.offset_minutes < 0 else "after"
    return f"{amount} {direction} {label}"


def _describe_minutes(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} min"


def occurrences_for_date(
    slots: Iterable[Slot],
    medications: Mapping[int, Medication],
    routine: Routine,
    day: date,
    tz: tzinfo,
) -> list[Occurrence]:
    """List every dose due on ``day``, earliest first.

    A dose is only due if the medicine is running on that date, the slot is
    switched on for that weekday, and the slot existed (and was not yet
    retired) at the scheduled moment. The last rule stops a medicine added at
    3 pm from instantly showing a "missed" 8 am dose.
    """
    found: list[Occurrence] = []
    for slot in slots:
        medication = medications.get(slot.medication_id)
        if medication is None or not _runs_on(medication, day):
            continue
        if slot.days_mask[day.weekday()] != "1":
            continue
        scheduled_at = datetime.combine(day, slot_time(slot, routine), tzinfo=tz)
        if scheduled_at < slot.created_at:
            continue
        if slot.retired_at is not None and scheduled_at >= slot.retired_at:
            continue
        found.append(Occurrence(slot, medication, day, scheduled_at))
    found.sort(key=lambda occ: (occ.scheduled_at, occ.medication.name.lower(), occ.slot.id))
    return found


def _runs_on(medication: Medication, day: date) -> bool:
    if day < medication.start_date:
        return False
    return medication.end_date is None or day <= medication.end_date


def doses_per_day(slots: Iterable[Slot]) -> float:
    """Average number of doses per day across a week for the given slots."""
    return sum(slot.days_mask.count("1") / 7 for slot in slots if slot.retired_at is None)
