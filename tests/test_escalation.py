"""The reminder ladder: who is told about a dose, and when."""

from __future__ import annotations

from datetime import timedelta

import pytest

from hearth.engine.escalation import (
    Stage,
    appointment_stage,
    due_actions,
    needs_checkin,
    plan,
    status_of,
)
from hearth.engine.models import Criticality, DoseStatus

from .factories import local, make_occurrence, skipped, snoozed, taken

EIGHT = local(2026, 11, 16, 8, 0)


def minutes(value: int) -> timedelta:
    return timedelta(minutes=value)


def stages(actions) -> list[Stage]:
    return [action.stage for action in actions]


# --- the ladder -----------------------------------------------------------


def test_critical_medicine_escalates_to_caretaker_then_family():
    ladder = plan(make_occurrence(EIGHT, Criticality.CRITICAL), None)
    assert [(a.stage, a.at) for a in ladder] == [
        (Stage.UPCOMING, EIGHT - minutes(10)),
        (Stage.NUDGE, EIGHT + minutes(20)),
        (Stage.CARETAKER, EIGHT + minutes(30)),
        (Stage.FAMILY, EIGHT + minutes(60)),
    ]


def test_important_medicine_reaches_the_caretaker_but_not_the_family():
    ladder = plan(make_occurrence(EIGHT, Criticality.IMPORTANT), None)
    assert stages(ladder) == [Stage.UPCOMING, Stage.NUDGE, Stage.CARETAKER]


def test_routine_medicine_never_alerts_anyone_but_the_elder():
    ladder = plan(make_occurrence(EIGHT, Criticality.ROUTINE), None)
    assert stages(ladder) == [Stage.UPCOMING, Stage.NUDGE]


@pytest.mark.parametrize("log", [taken(EIGHT), skipped(EIGHT)])
def test_nothing_is_planned_once_the_elder_has_decided(log):
    assert plan(make_occurrence(EIGHT, Criticality.CRITICAL), log) == []


# --- what fires right now -------------------------------------------------


def test_heads_up_fires_only_in_the_minutes_before_the_dose():
    occ = make_occurrence(EIGHT)
    assert stages(due_actions(occ, None, EIGHT - minutes(11))) == []
    assert stages(due_actions(occ, None, EIGHT - minutes(5))) == [Stage.UPCOMING]
    assert stages(due_actions(occ, None, EIGHT + minutes(1))) == []


def test_elder_is_nudged_before_anyone_else_hears():
    occ = make_occurrence(EIGHT, Criticality.CRITICAL)
    assert stages(due_actions(occ, None, EIGHT + minutes(25))) == [Stage.NUDGE]
    assert Stage.CARETAKER in stages(due_actions(occ, None, EIGHT + minutes(31)))
    assert Stage.FAMILY not in stages(due_actions(occ, None, EIGHT + minutes(31)))
    assert Stage.FAMILY in stages(due_actions(occ, None, EIGHT + minutes(61)))


def test_alerts_that_are_hours_overdue_are_dropped_rather_than_sent_late():
    occ = make_occurrence(EIGHT, Criticality.CRITICAL)
    assert due_actions(occ, None, EIGHT + timedelta(hours=5)) == []


def test_taking_the_dose_cancels_every_pending_alert():
    occ = make_occurrence(EIGHT, Criticality.CRITICAL)
    assert due_actions(occ, taken(EIGHT + minutes(25)), EIGHT + minutes(40)) == []


# --- snoozing -------------------------------------------------------------


def test_snooze_moves_the_nudge_and_the_rest_of_the_ladder():
    occ = make_occurrence(EIGHT, Criticality.CRITICAL)
    until = EIGHT + minutes(15)
    ladder = plan(occ, snoozed(EIGHT, until))
    assert [(a.stage, a.at) for a in ladder] == [
        (Stage.NUDGE, until),
        (Stage.CARETAKER, until + minutes(30)),
        (Stage.FAMILY, until + minutes(60)),
    ]


def test_repeated_snoozing_cannot_delay_alerts_for_more_than_an_hour():
    occ = make_occurrence(EIGHT, Criticality.CRITICAL)
    ladder = plan(occ, snoozed(EIGHT, EIGHT + timedelta(hours=4)))
    assert ladder[0].at == EIGHT + minutes(60)


def test_snooze_ending_before_the_dose_time_does_not_bring_alerts_forward():
    occ = make_occurrence(EIGHT, Criticality.IMPORTANT)
    ladder = plan(occ, snoozed(EIGHT - minutes(30), EIGHT - minutes(15)))
    assert ladder[0].at == EIGHT


# --- status ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset_minutes", "expected"),
    [
        (-30, DoseStatus.UPCOMING),
        (0, DoseStatus.DUE),
        (29, DoseStatus.DUE),
        (30, DoseStatus.LATE),
        (179, DoseStatus.LATE),
        (180, DoseStatus.MISSED),
    ],
)
def test_status_moves_from_upcoming_to_missed_as_time_passes(offset_minutes, expected):
    occ = make_occurrence(EIGHT, Criticality.IMPORTANT)
    assert status_of(occ, None, EIGHT + minutes(offset_minutes)) is expected


def test_status_reflects_what_was_logged():
    occ = make_occurrence(EIGHT)
    late = EIGHT + timedelta(hours=6)
    assert status_of(occ, taken(EIGHT), late) is DoseStatus.TAKEN
    assert status_of(occ, skipped(EIGHT), late) is DoseStatus.SKIPPED


def test_snoozed_status_expires_when_the_snooze_ends():
    occ = make_occurrence(EIGHT)
    log = snoozed(EIGHT, EIGHT + minutes(15))
    assert status_of(occ, log, EIGHT + minutes(10)) is DoseStatus.SNOOZED
    assert status_of(occ, log, EIGHT + minutes(16)) is DoseStatus.DUE


# --- appointments ---------------------------------------------------------


@pytest.mark.parametrize(
    ("hours_before", "expected"),
    [(30, None), (24, "24h"), (5, "24h"), (2, "2h"), (0.5, "2h"), (0, None), (-1, None)],
)
def test_appointment_reminder_stage(hours_before, expected):
    starts_at = local(2026, 11, 20, 10, 0)
    now = starts_at - timedelta(hours=hours_before)
    assert appointment_stage(starts_at, now) == expected


# --- quiet check-in -------------------------------------------------------


def test_checkin_respects_the_elders_choice_to_switch_it_off():
    now = local(2026, 11, 16, 14, 0)
    assert not needs_checkin(now, enabled=False, checkin_hour=12, last_activity=None)


def test_checkin_waits_until_the_chosen_hour():
    assert not needs_checkin(
        local(2026, 11, 16, 11, 59), enabled=True, checkin_hour=12, last_activity=None
    )
    assert needs_checkin(
        local(2026, 11, 16, 12, 0), enabled=True, checkin_hour=12, last_activity=None
    )


def test_checkin_is_not_needed_when_there_was_activity_today():
    now = local(2026, 11, 16, 14, 0)
    today = local(2026, 11, 16, 7, 30)
    yesterday = local(2026, 11, 15, 21, 0)
    assert not needs_checkin(now, enabled=True, checkin_hour=12, last_activity=today)
    assert needs_checkin(now, enabled=True, checkin_hour=12, last_activity=yesterday)
