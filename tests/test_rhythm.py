"""Learning the elder's real routine from confirmed doses."""

from __future__ import annotations

from datetime import time, timedelta

from hearth.engine.rhythm import Observation, shift_time, suggest_shifts

from .factories import local

BREAKFAST = {"breakfast": time(8, 0)}


def observations(gaps_in_minutes: list[int], group: str = "breakfast") -> list[Observation]:
    """One observation per day, each taken ``gap`` minutes after 8:00."""
    result = []
    for day, gap in enumerate(gaps_in_minutes, start=1):
        scheduled = local(2026, 11, day, 8, 0)
        result.append(Observation(group, scheduled, scheduled + timedelta(minutes=gap)))
    return result


def test_consistently_late_doses_suggest_a_later_time():
    [suggestion] = suggest_shifts(observations([38, 42, 40, 45, 36, 41]), BREAKFAST)
    assert suggestion.group == "breakfast"
    assert suggestion.current == time(8, 0)
    assert suggestion.suggested == time(8, 40)
    assert suggestion.shift_minutes == 40
    assert suggestion.samples == 6


def test_consistently_early_doses_suggest_an_earlier_time():
    [suggestion] = suggest_shifts(observations([-30, -25, -35, -30, -28]), BREAKFAST)
    assert suggestion.suggested == time(7, 30)


def test_a_few_days_are_not_yet_a_habit():
    assert suggest_shifts(observations([40, 40, 40, 40]), BREAKFAST) == []


def test_small_drifts_are_not_worth_a_change():
    assert suggest_shifts(observations([10, 12, 8, 15, 11, 9]), BREAKFAST) == []


def test_erratic_timing_is_not_treated_as_a_routine():
    assert suggest_shifts(observations([60, -50, 45, -40, 70, -5, 30]), BREAKFAST) == []


def test_doses_logged_hours_later_are_ignored():
    # Five real observations of ~40 minutes plus two entries logged in bulk at night.
    gaps = [40, 42, 38, 41, 39, 600, 720]
    [suggestion] = suggest_shifts(observations(gaps), BREAKFAST)
    assert suggestion.samples == 5
    assert suggestion.suggested == time(8, 40)


def test_suggested_time_is_rounded_to_five_minutes():
    [suggestion] = suggest_shifts(observations([33, 33, 33, 33, 33]), BREAKFAST)
    assert suggestion.suggested == time(8, 35)


def test_groups_without_a_current_time_are_skipped():
    assert suggest_shifts(observations([40] * 6, group="unknown"), BREAKFAST) == []


def test_each_group_is_judged_on_its_own():
    mixed = observations([40] * 6, "breakfast") + observations([2] * 6, "dinner")
    times = {"breakfast": time(8, 0), "dinner": time(19, 30)}
    assert [s.group for s in suggest_shifts(mixed, times)] == ["breakfast"]


def test_key_changes_when_the_suggestion_changes():
    [first] = suggest_shifts(observations([40] * 6), BREAKFAST)
    [second] = suggest_shifts(observations([60] * 6), BREAKFAST)
    assert first.key == "rhythm:breakfast:0840"
    assert first.key != second.key


def test_shift_time_stays_within_the_day():
    assert shift_time(time(23, 30), 90) == time(23, 59)
    assert shift_time(time(0, 20), -45) == time(0, 0)
