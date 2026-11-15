"""Plain data types shared by the decision engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum

#: Moments of the day a dose can be tied to. The elder sets the clock time of
#: each one, so "after breakfast" follows their real breakfast.
ANCHORS: tuple[str, ...] = ("wake", "breakfast", "lunch", "dinner", "bedtime")

#: Anchor value for a dose that is tied to an exact clock time instead.
FIXED = "fixed"

#: A routine maps each anchor name to the clock time it usually happens.
Routine = Mapping[str, time]

DEFAULT_ROUTINE: dict[str, time] = {
    "wake": time(7, 0),
    "breakfast": time(8, 0),
    "lunch": time(13, 0),
    "dinner": time(19, 30),
    "bedtime": time(22, 0),
}


class Criticality(StrEnum):
    """How much it matters if a dose is late. Chosen by the elder."""

    CRITICAL = "critical"
    IMPORTANT = "important"
    ROUTINE = "routine"


class DoseStatus(StrEnum):
    """Where a single dose stands right now."""

    TAKEN = "taken"
    SKIPPED = "skipped"
    SNOOZED = "snoozed"
    UPCOMING = "upcoming"
    DUE = "due"
    LATE = "late"
    MISSED = "missed"


@dataclass(frozen=True, slots=True)
class Medication:
    """A medicine on the elder's list."""

    id: int
    name: str
    dose: str
    criticality: Criticality
    start_date: date
    end_date: date | None = None
    instructions: str = ""
    missed_dose_rule: str = ""
    pill_color: str = "white"
    pill_shape: str = "round"
    stock_count: int | None = None
    units_per_dose: int = 1
    active: bool = True


@dataclass(frozen=True, slots=True)
class Slot:
    """One daily time a medicine is taken.

    A slot is never edited in place. When a schedule changes the old slot is
    retired and a new one is created, so the history of past doses stays true.
    """

    id: int
    medication_id: int
    anchor: str
    created_at: datetime
    offset_minutes: int = 0
    fixed_time: time | None = None
    days_mask: str = "1111111"  # Monday first; "1" means the dose is due that day
    retired_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Occurrence:
    """A specific dose on a specific day."""

    slot: Slot
    medication: Medication
    dose_date: date
    scheduled_at: datetime


@dataclass(frozen=True, slots=True)
class DoseLog:
    """What somebody recorded about a dose: taken, skipped or snoozed."""

    status: DoseStatus
    acted_at: datetime
    snoozed_until: datetime | None = None
    acted_by: int | None = None
