"""Care Weather: adherence summary and pattern spotting."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from hearth.engine.insights import (
    Adherence,
    DoseRecord,
    care_weather,
    find_patterns,
    summarise,
    weather_for,
)
from hearth.engine.models import DoseStatus

from .factories import local

TAKEN, SKIPPED, MISSED = DoseStatus.TAKEN, DoseStatus.SKIPPED, DoseStatus.MISSED


def record(
    when: datetime, status: DoseStatus = MISSED, name: str = "Metformin", med_id: int = 1
) -> DoseRecord:
    return DoseRecord(med_id, name, when, status)


def kinds(insights) -> set[str]:
    return {insight.kind for insight in insights}


def test_summarise_counts_each_outcome():
    records = [
        record(local(2026, 11, 2, 8), TAKEN),
        record(local(2026, 11, 2, 20), TAKEN),
        record(local(2026, 11, 3, 8), SKIPPED),
        record(local(2026, 11, 3, 20), MISSED),
    ]
    adherence = summarise(records)
    assert adherence == Adherence(taken=2, skipped=1, missed=1)
    assert adherence.total == 4
    assert adherence.rate == 50


def test_rate_is_unknown_when_nothing_was_scheduled():
    assert summarise([]).rate is None
    assert weather_for(Adherence()).code == "clear"


@pytest.mark.parametrize(
    ("taken", "missed", "code"),
    [(10, 0, "sunny"), (9, 1, "sunny"), (8, 2, "fair"), (6, 4, "cloudy"), (4, 6, "stormy")],
)
def test_weather_follows_the_adherence_rate(taken, missed, code):
    assert weather_for(Adherence(taken=taken, missed=missed)).code == code


def test_no_patterns_are_claimed_from_one_or_two_misses():
    misses = [record(local(2026, 11, 3, 20)), record(local(2026, 11, 10, 20))]
    assert find_patterns(misses) == []


def test_evening_misses_are_spotted():
    misses = [record(local(2026, 11, day, 19, 45)) for day in (2, 5, 9, 12)]
    misses.append(record(local(2026, 11, 6, 8)))
    insights = find_patterns(misses)
    evening = next(i for i in insights if i.kind == "time_of_day")
    assert "evening (4 of 5)" in evening.message
    assert "evening" in evening.suggestion


def test_hardest_weekday_is_spotted():
    tuesdays = [record(local(2026, 11, day, 20)) for day in (3, 10, 17)]
    other = [record(local(2026, 11, 5, 8))]
    weekday = next(i for i in find_patterns(tuesdays + other) if i.kind == "weekday")
    assert weekday.message.startswith("Tuesdays are the hardest day (3 of 4")


def test_most_missed_medicine_is_spotted_only_when_there_are_several_medicines():
    statin = [record(local(2026, 11, day, 22), name="Atorvastatin", med_id=2) for day in (2, 4, 6)]
    metformin = [record(local(2026, 11, 3, 8))]
    assert "medication" in kinds(find_patterns(statin + metformin))
    assert "medication" not in kinds(find_patterns(statin))


def test_misses_on_appointment_days_are_spotted():
    visit_day = date(2026, 11, 12)
    misses = [record(local(2026, 11, 12, 8)), record(local(2026, 11, 12, 20))]
    [insight] = find_patterns(misses, appointment_days={visit_day})
    assert insight.kind == "appointment_day"
    assert insight.message.startswith("2 doses were missed")


def test_a_medicine_skipped_repeatedly_is_flagged_for_the_doctor():
    skips = [record(local(2026, 11, day, 22), SKIPPED, "Atorvastatin") for day in (2, 4, 6)]
    [insight] = find_patterns(skips)
    assert insight.kind == "skipped"
    assert "Atorvastatin was skipped by choice 3 times" in insight.message


def test_a_fully_confirmed_week_gets_a_positive_note():
    week = [record(local(2026, 11, day, 8), TAKEN) for day in range(2, 9)]
    assert kinds(find_patterns(week)) == {"steady"}


def test_care_weather_combines_headline_weather_and_patterns():
    week = [record(local(2026, 11, day, 8), TAKEN) for day in range(9, 16)]
    week += [record(local(2026, 11, 10, 20)), record(local(2026, 11, 11, 20), SKIPPED)]
    history = week + [record(local(2026, 11, day, 20)) for day in (3, 5)]
    report = care_weather(week, history, appointment_days=(), name="Kamala")
    assert report.weather.code == "fair"
    assert report.headline == "Kamala took 7 of 9 doses (78%), 1 missed, 1 skipped by choice."
    assert "time_of_day" in kinds(report.insights)


def test_care_weather_handles_an_empty_week():
    report = care_weather([], [], appointment_days=(), name="Kamala")
    assert report.headline == "No doses were scheduled for Kamala this week."
    assert report.insights == ()
