"""The day-to-day care picture: today's doses, history, insights and logging."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..access import Role
from ..db import transaction
from ..engine.escalation import status_of
from ..engine.insights import CareWeather, DoseRecord, care_weather
from ..engine.models import ANCHORS, FIXED, DoseLog, DoseStatus, Medication, Occurrence, Slot
from ..engine.refill import RefillForecast, forecast_refill
from ..engine.rhythm import Observation, RhythmSuggestion, suggest_shifts
from ..engine.schedule import describe_slot, occurrences_for_date, slot_time
from ..repositories import appointments, doses, medications, profiles, records
from ..repositories.profiles import Member, Profile
from ..timeutil import format_clock, get_zone

SNOOZE_MINUTES = 15
WEEK_DAYS = 7
PATTERN_DAYS = 28
RHYTHM_DAYS = 14

#: Outcomes that are settled and therefore count towards adherence.
_FINAL = (DoseStatus.TAKEN, DoseStatus.SKIPPED, DoseStatus.MISSED)

DOSE_ACTIONS: tuple[str, ...] = ("take", "skip", "snooze", "undo")


class DoseError(Exception):
    """A dose action that cannot be carried out, with a message for the user."""


@dataclass(frozen=True, slots=True)
class CareContext:
    """Everything needed to work out an elder's doses, loaded once per request."""

    profile: Profile
    tz: ZoneInfo
    medications: dict[int, Medication]
    slots: list[Slot]

    def local(self, moment: datetime) -> datetime:
        """Convert a moment to the elder's own timezone."""
        return moment.astimezone(self.tz)

    def occurrences(self, day: date) -> list[Occurrence]:
        return occurrences_for_date(
            self.slots, self.medications, self.profile.routine, day, self.tz
        )

    def active_slots(self, medication_id: int) -> list[Slot]:
        return [
            slot
            for slot in self.slots
            if slot.medication_id == medication_id and slot.retired_at is None
        ]


@dataclass(frozen=True, slots=True)
class DoseView:
    """One dose as shown on screen."""

    occurrence: Occurrence
    log: DoseLog | None
    status: DoseStatus
    when_label: str


@dataclass(frozen=True, slots=True)
class RhythmView:
    """A routine suggestion with wording ready for display."""

    suggestion: RhythmSuggestion
    label: str


def load_context(db: sqlite3.Connection, profile: Profile) -> CareContext:
    meds = medications.list_medications(db, profile.id, include_archived=True)
    return CareContext(
        profile=profile,
        tz=get_zone(profile.timezone),
        medications={med.id: med for med in meds},
        slots=medications.list_slots(db, profile.id),
    )


# --- reading --------------------------------------------------------------


def doses_for_day(
    db: sqlite3.Connection, ctx: CareContext, day: date, now: datetime
) -> list[DoseView]:
    logs = doses.logs_between(db, ctx.profile.id, day, day)
    views = []
    for occ in ctx.occurrences(day):
        log = logs.get((occ.slot.id, day))
        if not occ.medication.active and log is None:
            continue  # a stopped medicine keeps its history but asks for nothing more
        views.append(DoseView(occ, log, status_of(occ, log, now), describe_slot(occ.slot)))
    return views


def outcomes_between(
    db: sqlite3.Connection, ctx: CareContext, first: date, last: date, now: datetime
) -> list[DoseRecord]:
    """Settled doses (taken, skipped or missed) in a date range, inclusive."""
    logs = doses.logs_between(db, ctx.profile.id, first, last)
    found: list[DoseRecord] = []
    for day in _days(first, last):
        for occ in ctx.occurrences(day):
            status = status_of(occ, logs.get((occ.slot.id, day)), now)
            if status in _FINAL:
                found.append(
                    DoseRecord(occ.medication.id, occ.medication.name, occ.scheduled_at, status)
                )
    return found


def care_weather_report(db: sqlite3.Connection, ctx: CareContext, now: datetime) -> CareWeather:
    """Build the weekly Care Weather report as of ``now``."""
    today = ctx.local(now).date()
    history = outcomes_between(db, ctx, today - timedelta(days=PATTERN_DAYS - 1), today, now)
    week_start = today - timedelta(days=WEEK_DAYS - 1)
    week = [record for record in history if record.scheduled_at.date() >= week_start]
    visits = appointments.list_between(db, ctx.profile.id, now - timedelta(days=PATTERN_DAYS), now)
    visit_days = {ctx.local(visit.starts_at).date() for visit in visits}
    return care_weather(week, history, visit_days, ctx.profile.elder_name)


def refill_forecasts(ctx: CareContext, today: date) -> list[RefillForecast]:
    """Run-out forecasts for every active medicine with tracked stock."""
    forecasts = []
    for med in ctx.medications.values():
        if not med.active:
            continue
        forecast = forecast_refill(med, ctx.active_slots(med.id), today)
        if forecast is not None:
            forecasts.append(forecast)
    return sorted(forecasts, key=lambda f: (f.days_left, f.name.lower()))


def last_activity(db: sqlite3.Connection, profile: Profile) -> datetime | None:
    """The latest sign that the elder is up and about.

    Either the elder used Hearth, or someone confirmed a dose was taken.
    """
    moments = [profile.last_elder_activity_at, doses.last_taken_at(db, profile.id)]
    known = [moment for moment in moments if moment is not None]
    return max(known) if known else None


# --- routine learning -----------------------------------------------------


def rhythm_suggestions(db: sqlite3.Connection, ctx: CareContext, now: datetime) -> list[RhythmView]:
    """Routine changes worth offering to the elder, minus any they dismissed."""
    today = ctx.local(now).date()
    first = today - timedelta(days=RHYTHM_DAYS - 1)
    logs = doses.logs_between(db, ctx.profile.id, first, today)

    observations: list[Observation] = []
    for day in _days(first, today):
        for occ in ctx.occurrences(day):
            log = logs.get((occ.slot.id, day))
            if log is not None and log.status is DoseStatus.TAKEN and occ.slot.retired_at is None:
                observations.append(
                    Observation(_rhythm_group(occ.slot), occ.scheduled_at, ctx.local(log.acted_at))
                )

    dismissed = profiles.dismissed_suggestion_keys(db, ctx.profile.id)
    return [
        RhythmView(suggestion, _rhythm_label(ctx, suggestion.group))
        for suggestion in suggest_shifts(observations, _current_times(ctx))
        if suggestion.key not in dismissed
    ]


def apply_rhythm_suggestion(
    db: sqlite3.Connection, ctx: CareContext, key: str, member: Member, now: datetime
) -> bool:
    """Apply a suggestion the elder accepted. Returns False if it no longer applies."""
    match = next((v for v in rhythm_suggestions(db, ctx, now) if v.suggestion.key == key), None)
    if match is None:
        return False
    suggestion = match.suggestion
    with transaction(db):
        if suggestion.group in ANCHORS:
            routine = {**ctx.profile.routine, suggestion.group: suggestion.suggested}
            profiles.update_routine(db, ctx.profile.id, routine)
        else:
            slot_id = int(suggestion.group.removeprefix("slot:"))
            medications.set_slot_time(
                db, ctx.profile.id, slot_id, format_clock(suggestion.suggested), now
            )
        records.add_audit(
            db,
            profile_id=ctx.profile.id,
            member_id=member.id,
            action="Moved a reminder time",
            detail=(
                f"{match.label}: {format_clock(suggestion.current)}"
                f" to {format_clock(suggestion.suggested)}"
            ),
            now=now,
        )
    return True


def _rhythm_group(slot: Slot) -> str:
    return slot.anchor if slot.anchor != FIXED else f"slot:{slot.id}"


def _current_times(ctx: CareContext) -> dict[str, time]:
    times: dict[str, time] = dict(ctx.profile.routine)
    for slot in ctx.slots:
        if slot.anchor == FIXED and slot.retired_at is None:
            times[_rhythm_group(slot)] = slot_time(slot, ctx.profile.routine)
    return times


def _rhythm_label(ctx: CareContext, group: str) -> str:
    if group in ANCHORS:
        return {"wake": "Waking up", "bedtime": "Bedtime"}.get(group, group.capitalize())
    slot_id = int(group.removeprefix("slot:"))
    slot = next(slot for slot in ctx.slots if slot.id == slot_id)
    return ctx.medications[slot.medication_id].name


# --- logging doses --------------------------------------------------------


def record_dose(
    db: sqlite3.Connection,
    ctx: CareContext,
    member: Member,
    *,
    slot_id: int,
    dose_date: date,
    action: str,
    now: datetime,
) -> Occurrence:
    """Record that a dose was taken, skipped, snoozed, or undo that record.

    Only real doses from today or yesterday can be changed, and the tracked
    stock is kept in step with what was actually taken.
    """
    if action not in DOSE_ACTIONS:
        raise DoseError("That action is not available.")
    today = ctx.local(now).date()
    if not today - timedelta(days=1) <= dose_date <= today:
        raise DoseError("Only doses from today or yesterday can be changed.")
    occurrence = next((o for o in ctx.occurrences(dose_date) if o.slot.id == slot_id), None)
    if occurrence is None:
        raise DoseError("That dose could not be found.")

    previous = doses.get_log(db, ctx.profile.id, slot_id, dose_date)
    was_taken = previous is not None and previous.status is DoseStatus.TAKEN
    decided = previous is not None and previous.status is not DoseStatus.SNOOZED
    if action == "snooze" and decided:
        raise DoseError("That dose has already been recorded.")

    medication = occurrence.medication
    with transaction(db):
        if action == "undo":
            doses.delete_log(db, ctx.profile.id, slot_id, dose_date)
        else:
            status, snoozed_until = _status_for(action, now)
            doses.save_log(
                db,
                profile_id=ctx.profile.id,
                slot_id=slot_id,
                dose_date=dose_date,
                status=status,
                acted_at=now,
                acted_by=member.id,
                snoozed_until=snoozed_until,
            )
        now_taken = action == "take"
        if now_taken != was_taken:
            delta = -medication.units_per_dose if now_taken else medication.units_per_dose
            medications.adjust_stock(db, ctx.profile.id, medication.id, delta)
        records.add_audit(
            db,
            profile_id=ctx.profile.id,
            member_id=member.id,
            action=_AUDIT_ACTIONS[action],
            detail=f"{medication.name} {medication.dose}".strip()
            + f", due {occurrence.scheduled_at:%d %b %H:%M}",
            now=now,
        )
        if member.role is Role.ELDER:
            profiles.touch_elder_activity(db, ctx.profile.id, now)
    return occurrence


_AUDIT_ACTIONS = {
    "take": "Confirmed a dose as taken",
    "skip": "Chose to skip a dose",
    "snooze": f"Asked for a reminder in {SNOOZE_MINUTES} minutes",
    "undo": "Cleared a dose record",
}


def _status_for(action: str, now: datetime) -> tuple[DoseStatus, datetime | None]:
    if action == "take":
        return DoseStatus.TAKEN, None
    if action == "skip":
        return DoseStatus.SKIPPED, None
    return DoseStatus.SNOOZED, now + timedelta(minutes=SNOOZE_MINUTES)


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
