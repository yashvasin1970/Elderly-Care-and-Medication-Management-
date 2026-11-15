"""Validation of submitted forms.

Every value from a browser is checked here before it reaches the database.
Each ``validate_*`` function returns the cleaned data (or ``None``) together
with a dict of field errors, worded so the person knows how to fix them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo

from ..access import ASSIGNABLE_ROLES, Role
from ..engine.models import ANCHORS, FIXED, Criticality
from ..engine.parsing import MedicationDraft
from ..repositories.appointments import AppointmentInput
from ..repositories.medications import MedicationInput, SlotInput
from ..repositories.records import JOURNAL_KINDS
from ..timeutil import format_clock, is_valid_zone, parse_clock, parse_date

Errors = dict[str, str]
Form = Mapping[str, str]

MAX_SLOTS = 4
PILL_COLORS = (
    "white",
    "yellow",
    "orange",
    "pink",
    "red",
    "purple",
    "blue",
    "green",
    "brown",
    "grey",
)
PILL_SHAPES = ("round", "oval", "capsule", "liquid", "injection", "inhaler", "drops", "patch")
ANCHOR_CHOICES = (
    ("wake", "Waking up"),
    ("breakfast", "Breakfast"),
    ("lunch", "Lunch"),
    ("dinner", "Dinner"),
    ("bedtime", "Bedtime"),
    (FIXED, "An exact time"),
)
OFFSET_CHOICES = (
    (-60, "1 hour before"),
    (-30, "30 minutes before"),
    (0, "At that time"),
    (15, "15 minutes after"),
    (30, "30 minutes after"),
    (60, "1 hour after"),
)
CRITICALITY_CHOICES = (
    (
        "critical",
        "Critical",
        "If a dose is late, alert my caretaker after 30 minutes and my family after 1 hour.",
    ),
    ("important", "Important", "If a dose is late, alert my caretaker after 1 hour."),
    ("routine", "Routine", "Never alert anyone. Include it in the weekly summary only."),
)
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OFFSETS = {offset for offset, _ in OFFSET_CHOICES}
_ROUTINE_ORDER_ERROR = "Times should run in order through the day, from waking up to bedtime."


@dataclass(frozen=True, slots=True)
class MemberInput:
    name: str
    email: str
    role: Role
    trusted_editor: bool
    receives_alerts: bool


@dataclass(frozen=True, slots=True)
class SettingsInput:
    elder_name: str
    email: str
    timezone: str
    checkin_enabled: bool
    checkin_hour: int
    receives_alerts: bool


# --- small helpers --------------------------------------------------------


def clean_line(form: Form, field: str, limit: int) -> str:
    """A single-line text value with whitespace tidied and length capped."""
    return " ".join(form.get(field, "").split())[:limit]


def clean_text(form: Form, field: str, limit: int) -> str:
    """A multi-line text value, trimmed and length capped."""
    return form.get(field, "").replace("\r\n", "\n").strip()[:limit]


def is_email(value: str) -> bool:
    return len(value) <= 254 and bool(_EMAIL.match(value))


def _whole_number(raw: str, low: int, high: int) -> int | None:
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if low <= value <= high else None


# --- medicines ------------------------------------------------------------


def validate_medication(form: Form, today: date) -> tuple[MedicationInput | None, Errors]:
    errors: Errors = {}

    name = clean_line(form, "name", 80)
    if not name:
        errors["name"] = "Enter the name of the medicine."

    criticality = form.get("criticality", "")
    if criticality not in {level.value for level in Criticality}:
        errors["criticality"] = "Choose how important this medicine is."

    stock_count: int | None = None
    if form.get("stock_count", "").strip():
        stock_count = _whole_number(form["stock_count"], 0, 9999)
        if stock_count is None:
            errors["stock_count"] = "Enter a whole number from 0 to 9999, or leave it empty."

    units_per_dose = _whole_number(form.get("units_per_dose", "1") or "1", 1, 20)
    if units_per_dose is None:
        errors["units_per_dose"] = "Enter a whole number from 1 to 20."

    start_date, end_date = _course_dates(form, today, errors)

    days_mask = "".join("1" if form.get(f"day-{index}") else "0" for index in range(7))
    if "1" not in days_mask:
        errors["days"] = "Choose at least one day of the week."

    slots = _slots(form, errors)

    if errors:
        return None, errors
    color, shape = form.get("pill_color", ""), form.get("pill_shape", "")
    return (
        MedicationInput(
            name=name,
            dose=clean_line(form, "dose", 40),
            criticality=criticality,
            instructions=clean_line(form, "instructions", 200),
            missed_dose_rule=clean_line(form, "missed_dose_rule", 300),
            pill_color=color if color in PILL_COLORS else PILL_COLORS[0],
            pill_shape=shape if shape in PILL_SHAPES else PILL_SHAPES[0],
            stock_count=stock_count,
            units_per_dose=units_per_dose or 1,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat() if end_date else None,
            days_mask=days_mask,
            slots=slots,
        ),
        errors,
    )


def _course_dates(form: Form, today: date, errors: Errors) -> tuple[date, date | None]:
    start_date, end_date = today, None
    if form.get("start_date", "").strip():
        try:
            start_date = parse_date(form["start_date"])
        except ValueError:
            errors["start_date"] = "Enter the start date as year-month-day, like 2026-11-16."
    if form.get("end_date", "").strip():
        try:
            end_date = parse_date(form["end_date"])
        except ValueError:
            errors["end_date"] = "Enter the end date as year-month-day, or leave it empty."
        else:
            if end_date < start_date:
                errors["end_date"] = "The end date cannot be before the start date."
    return start_date, end_date


def _slots(form: Form, errors: Errors) -> tuple[SlotInput, ...]:
    slots: list[SlotInput] = []
    for index in range(MAX_SLOTS):
        anchor = form.get(f"slot-{index}-anchor", "")
        if not anchor:
            continue
        slot: SlotInput | None = None
        if anchor == FIXED:
            try:
                fixed = parse_clock(form.get(f"slot-{index}-time", ""))
            except ValueError:
                errors["slots"] = "For an exact time, enter it as hours and minutes, like 21:30."
            else:
                slot = SlotInput(FIXED, 0, format_clock(fixed))
        elif anchor in ANCHORS:
            offset = _whole_number(form.get(f"slot-{index}-offset", "0") or "0", -120, 120)
            slot = SlotInput(anchor, offset if offset in _OFFSETS else 0)
        else:
            errors["slots"] = "Choose a time of day from the list."
        if slot is not None and slot not in slots:
            slots.append(slot)
    if not slots and "slots" not in errors:
        errors["slots"] = "Choose at least one time of day."
    return tuple(slots)


def medication_form_values(data: MedicationInput) -> dict[str, str]:
    """Flatten a medicine into the field names the form uses."""
    values = {
        "name": data.name,
        "dose": data.dose,
        "criticality": data.criticality,
        "instructions": data.instructions,
        "missed_dose_rule": data.missed_dose_rule,
        "pill_color": data.pill_color,
        "pill_shape": data.pill_shape,
        "stock_count": "" if data.stock_count is None else str(data.stock_count),
        "units_per_dose": str(data.units_per_dose),
        "start_date": data.start_date,
        "end_date": data.end_date or "",
    }
    for index, flag in enumerate(data.days_mask):
        if flag == "1":
            values[f"day-{index}"] = "1"
    for index, slot in enumerate(data.slots[:MAX_SLOTS]):
        values[f"slot-{index}-anchor"] = slot.anchor
        values[f"slot-{index}-offset"] = str(slot.offset_minutes)
        values[f"slot-{index}-time"] = slot.fixed_time or ""
    return values


def blank_medication_values(today: date) -> dict[str, str]:
    """Sensible starting values for a new medicine."""
    values = {
        "criticality": Criticality.IMPORTANT.value,
        "units_per_dose": "1",
        "start_date": today.isoformat(),
        "slot-0-anchor": "breakfast",
        "slot-0-offset": "0",
    }
    values.update({f"day-{index}": "1" for index in range(7)})
    return values


def draft_form_values(draft: MedicationDraft, today: date) -> dict[str, str]:
    """Pre-fill the form from a sentence that was read by the parser."""
    values = blank_medication_values(today)
    values.update({"name": draft.name, "dose": draft.dose, "instructions": draft.instructions})
    if draft.duration_days:
        values["end_date"] = date.fromordinal(
            today.toordinal() + draft.duration_days - 1
        ).isoformat()
    for index, slot in enumerate(draft.slots[:MAX_SLOTS]):
        values[f"slot-{index}-anchor"] = slot.anchor
        values[f"slot-{index}-offset"] = str(slot.offset_minutes)
        values[f"slot-{index}-time"] = slot.fixed_time or ""
    return values


# --- appointments ---------------------------------------------------------


def validate_appointment(form: Form, tz: tzinfo) -> tuple[AppointmentInput | None, Errors]:
    errors: Errors = {}
    title = clean_line(form, "title", 100)
    if not title:
        errors["title"] = 'Say what the appointment is for, like "Heart check-up".'
    starts_at: datetime | None = None
    try:
        starts_at = datetime.strptime(form.get("starts_at", "").strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        errors["starts_at"] = "Choose the date and time of the appointment."
    if errors or starts_at is None:
        return None, errors
    return (
        AppointmentInput(
            title=title,
            doctor=clean_line(form, "doctor", 80),
            location=clean_line(form, "location", 120),
            starts_at=starts_at.replace(tzinfo=tz),
            notes=clean_text(form, "notes", 500),
        ),
        errors,
    )


# --- circle members -------------------------------------------------------


def validate_member(form: Form) -> tuple[MemberInput | None, Errors]:
    errors: Errors = {}
    name = clean_line(form, "name", 60)
    if not name:
        errors["name"] = "Enter this person's name."
    email = clean_line(form, "email", 254)
    if email and not is_email(email):
        errors["email"] = "That email address does not look right. Check it, or leave it empty."
    try:
        role = Role(form.get("role", ""))
    except ValueError:
        role = Role.VIEWER
        errors["role"] = "Choose what this person can do."
    else:
        if role not in ASSIGNABLE_ROLES:
            errors["role"] = "Choose what this person can do."
    if errors:
        return None, errors
    can_edit = role in (Role.CARETAKER, Role.FAMILY)
    return (
        MemberInput(
            name=name,
            email=email,
            role=role,
            trusted_editor=can_edit and bool(form.get("trusted_editor")),
            receives_alerts=role is not Role.VIEWER and bool(form.get("receives_alerts")),
        ),
        errors,
    )


# --- routine and settings -------------------------------------------------


def validate_routine(form: Form) -> tuple[dict[str, time] | None, Errors]:
    errors: Errors = {}
    routine: dict[str, time] = {}
    for anchor in ANCHORS:
        try:
            routine[anchor] = parse_clock(form.get(anchor, ""))
        except ValueError:
            errors[anchor] = "Enter a time as hours and minutes, like 08:00."
    if not errors:
        times = [routine[anchor] for anchor in ANCHORS]
        if times != sorted(times) or len(set(times)) != len(times):
            errors["order"] = _ROUTINE_ORDER_ERROR
    return (None, errors) if errors else (routine, errors)


def validate_settings(form: Form) -> tuple[SettingsInput | None, Errors]:
    errors: Errors = {}
    elder_name = clean_line(form, "elder_name", 60)
    if not elder_name:
        errors["elder_name"] = "Enter your name."
    email = clean_line(form, "email", 254)
    if email and not is_email(email):
        errors["email"] = "That email address does not look right. Check it, or leave it empty."
    timezone = form.get("timezone", "")
    if not is_valid_zone(timezone):
        errors["timezone"] = "Choose a timezone from the list."
    checkin_hour = _whole_number(form.get("checkin_hour", "12") or "12", 0, 23)
    if checkin_hour is None:
        errors["checkin_hour"] = "Choose an hour of the day."
    if errors:
        return None, errors
    return (
        SettingsInput(
            elder_name=elder_name,
            email=email,
            timezone=timezone,
            checkin_enabled=bool(form.get("checkin_enabled")),
            checkin_hour=checkin_hour or 0,
            receives_alerts=bool(form.get("receives_alerts")),
        ),
        errors,
    )


def validate_journal(form: Form) -> tuple[tuple[str, str] | None, Errors]:
    """Return ``(kind, body)`` for a journal entry."""
    kind = form.get("kind", "")
    body = clean_text(form, "body", 1000)
    errors: Errors = {}
    if kind not in JOURNAL_KINDS:
        errors["kind"] = "Choose what kind of note this is."
    if not body:
        errors["body"] = "Write a few words first."
    return (None, errors) if errors else ((kind, body), errors)
