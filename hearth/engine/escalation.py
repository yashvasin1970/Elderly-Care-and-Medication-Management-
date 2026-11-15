"""Decide who should be told about a dose or appointment, and when.

The rule of thumb: the elder always hears first. Other people are only
alerted if a dose stays unconfirmed, and how quickly that happens depends on
how critical the elder has said the medicine is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .models import Criticality, DoseLog, DoseStatus, Occurrence


class Stage(StrEnum):
    """A step on the reminder ladder."""

    UPCOMING = "upcoming"  # heads-up to the elder shortly before the dose
    NUDGE = "nudge"  # second reminder to the elder
    CARETAKER = "caretaker"  # tell the caretakers
    FAMILY = "family"  # tell the family


@dataclass(frozen=True, slots=True)
class EscalationPolicy:
    """Delays, measured from the scheduled time, for each later stage."""

    nudge_after: timedelta
    caretaker_after: timedelta | None
    family_after: timedelta | None


POLICIES: dict[Criticality, EscalationPolicy] = {
    Criticality.CRITICAL: EscalationPolicy(
        nudge_after=timedelta(minutes=20),
        caretaker_after=timedelta(minutes=30),
        family_after=timedelta(minutes=60),
    ),
    Criticality.IMPORTANT: EscalationPolicy(
        nudge_after=timedelta(minutes=30),
        caretaker_after=timedelta(minutes=60),
        family_after=None,
    ),
    # Routine medicines never raise an alert; misses show in the weekly summary.
    Criticality.ROUTINE: EscalationPolicy(
        nudge_after=timedelta(minutes=60),
        caretaker_after=None,
        family_after=None,
    ),
}

#: How long before a dose the elder gets a heads-up.
UPCOMING_LEAD = timedelta(minutes=10)
#: Snoozing can push the ladder back by at most this much.
MAX_SNOOZE_SHIFT = timedelta(minutes=60)
#: An alert that could not be sent within this window is dropped, not sent late.
STALE_AFTER = timedelta(hours=3)
#: An unconfirmed dose counts as missed after this long.
MISSED_AFTER = timedelta(hours=3)


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """A reminder stage and the moment it becomes due."""

    stage: Stage
    at: datetime


def plan(occurrence: Occurrence, log: DoseLog | None) -> list[PlannedAction]:
    """Lay out the full reminder ladder for one dose.

    Returns nothing once the dose is taken or skipped: skipping is the
    elder's decision, so it is reported in summaries rather than alerted on.
    """
    if log is not None and log.status in (DoseStatus.TAKEN, DoseStatus.SKIPPED):
        return []

    policy = POLICIES[occurrence.medication.criticality]
    scheduled = occurrence.scheduled_at
    actions: list[PlannedAction] = []

    snooze_end = _snooze_end(occurrence, log)
    if snooze_end is None:
        actions.append(PlannedAction(Stage.UPCOMING, scheduled - UPCOMING_LEAD))
        actions.append(PlannedAction(Stage.NUDGE, scheduled + policy.nudge_after))
        base = scheduled
    else:
        # The elder asked to be reminded later: nudge when the snooze ends
        # and measure the rest of the ladder from there.
        actions.append(PlannedAction(Stage.NUDGE, snooze_end))
        base = snooze_end

    if policy.caretaker_after is not None:
        actions.append(PlannedAction(Stage.CARETAKER, base + policy.caretaker_after))
    if policy.family_after is not None:
        actions.append(PlannedAction(Stage.FAMILY, base + policy.family_after))
    return actions


def due_actions(occurrence: Occurrence, log: DoseLog | None, now: datetime) -> list[PlannedAction]:
    """Return the stages that should fire at ``now``.

    Each stage has a window. A heads-up is pointless once the dose time has
    passed, and an alert that is hours overdue (say the job was down) is
    dropped instead of arriving out of context.
    """
    due: list[PlannedAction] = []
    for action in plan(occurrence, log):
        if action.stage is Stage.UPCOMING:
            expires = occurrence.scheduled_at
        else:
            expires = action.at + STALE_AFTER
        if action.at <= now < expires:
            due.append(action)
    return due


def status_of(occurrence: Occurrence, log: DoseLog | None, now: datetime) -> DoseStatus:
    """Work out the current status of a dose for display and statistics."""
    if log is not None:
        if log.status in (DoseStatus.TAKEN, DoseStatus.SKIPPED):
            return log.status
        if (
            log.status is DoseStatus.SNOOZED
            and log.snoozed_until is not None
            and now < log.snoozed_until
        ):
            return DoseStatus.SNOOZED
    if now < occurrence.scheduled_at:
        return DoseStatus.UPCOMING
    elapsed = now - occurrence.scheduled_at
    if elapsed >= MISSED_AFTER:
        return DoseStatus.MISSED
    if elapsed >= POLICIES[occurrence.medication.criticality].nudge_after:
        return DoseStatus.LATE
    return DoseStatus.DUE


def _snooze_end(occurrence: Occurrence, log: DoseLog | None) -> datetime | None:
    """When an active snooze ends, capped so it cannot delay alerts for ever."""
    if log is None or log.status is not DoseStatus.SNOOZED or log.snoozed_until is None:
        return None
    latest = occurrence.scheduled_at + MAX_SNOOZE_SHIFT
    return max(occurrence.scheduled_at, min(log.snoozed_until, latest))


#: Appointment reminders, nearest first: (name, how long before the visit).
APPOINTMENT_STAGES: tuple[tuple[str, timedelta], ...] = (
    ("2h", timedelta(hours=2)),
    ("24h", timedelta(hours=24)),
)


def appointment_stage(starts_at: datetime, now: datetime) -> str | None:
    """Return the single appointment reminder that applies at ``now``.

    Only the nearest stage is returned, so an appointment booked an hour
    ahead produces one reminder rather than two at once.
    """
    if now >= starts_at:
        return None
    for name, lead in APPOINTMENT_STAGES:
        if now >= starts_at - lead:
            return name
    return None


def needs_checkin(
    now: datetime,
    *,
    enabled: bool,
    checkin_hour: int,
    last_activity: datetime | None,
) -> bool:
    """Should the circle get a gentle "we haven't heard from you" note?

    True when the elder has opted in, it is past their chosen hour, and there
    has been no sign of activity since midnight. All times are in the elder's
    own timezone.
    """
    if not enabled or now.hour < checkin_hour:
        return False
    if last_activity is None:
        return True
    return last_activity.astimezone(now.tzinfo).date() < now.date()
