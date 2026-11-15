"""Turning slots into the doses due on a day."""

from __future__ import annotations

from datetime import date, time

import pytest

from hearth.engine.schedule import describe_slot, doses_per_day, occurrences_for_date, slot_time

from .factories import ROUTINE, TZ, local, make_medication, make_slot

MONDAY = date(2026, 11, 16)


def test_anchored_slot_follows_the_routine_plus_offset():
    slot = make_slot(anchor="breakfast", offset_minutes=15)
    assert slot_time(slot, ROUTINE) == time(8, 15)


def test_anchored_slot_moves_when_the_routine_changes():
    slot = make_slot(anchor="breakfast", offset_minutes=15)
    later_breakfast = {**ROUTINE, "breakfast": time(8, 40)}
    assert slot_time(slot, later_breakfast) == time(8, 55)


def test_fixed_slot_uses_its_own_time():
    slot = make_slot(anchor="fixed", fixed_time=time(21, 30))
    assert slot_time(slot, ROUTINE) == time(21, 30)


def test_fixed_slot_without_a_time_is_rejected():
    with pytest.raises(ValueError, match="fixed_time"):
        slot_time(make_slot(anchor="fixed"), ROUTINE)


def test_offset_never_spills_past_midnight():
    late_night = {**ROUTINE, "bedtime": time(23, 30)}
    slot = make_slot(anchor="bedtime", offset_minutes=60)
    assert slot_time(slot, late_night) == time(23, 59)


def test_occurrences_are_sorted_by_time():
    medications = {1: make_medication(1, "Metformin"), 2: make_medication(2, "Insulin")}
    slots = [
        make_slot(1, 1, "dinner"),
        make_slot(2, 2, "bedtime"),
        make_slot(3, 1, "breakfast"),
    ]
    found = occurrences_for_date(slots, medications, ROUTINE, MONDAY, TZ)
    assert [occ.slot.id for occ in found] == [3, 1, 2]
    assert found[0].scheduled_at == local(2026, 11, 16, 8, 0)


def test_days_switched_off_in_the_mask_are_skipped():
    medications = {1: make_medication()}
    weekdays_only = make_slot(days_mask="1111100")
    saturday = date(2026, 11, 21)
    assert occurrences_for_date([weekdays_only], medications, ROUTINE, MONDAY, TZ)
    assert occurrences_for_date([weekdays_only], medications, ROUTINE, saturday, TZ) == []


def test_medicine_is_only_due_between_its_start_and_end_dates():
    course = make_medication(start_date=date(2026, 11, 10), end_date=date(2026, 11, 16))
    slots = [make_slot()]
    meds = {1: course}
    assert occurrences_for_date(slots, meds, ROUTINE, date(2026, 11, 9), TZ) == []
    assert len(occurrences_for_date(slots, meds, ROUTINE, date(2026, 11, 16), TZ)) == 1
    assert occurrences_for_date(slots, meds, ROUTINE, date(2026, 11, 17), TZ) == []


def test_a_slot_added_in_the_afternoon_does_not_create_a_missed_morning_dose():
    slot = make_slot(anchor="breakfast", created_at=local(2026, 11, 16, 15, 0))
    meds = {1: make_medication()}
    assert occurrences_for_date([slot], meds, ROUTINE, MONDAY, TZ) == []
    assert len(occurrences_for_date([slot], meds, ROUTINE, date(2026, 11, 17), TZ)) == 1


def test_a_retired_slot_keeps_earlier_doses_but_drops_later_ones():
    retired_at_noon = local(2026, 11, 16, 12, 0)
    morning = make_slot(1, anchor="breakfast", retired_at=retired_at_noon)
    evening = make_slot(2, anchor="dinner", retired_at=retired_at_noon)
    found = occurrences_for_date([morning, evening], {1: make_medication()}, ROUTINE, MONDAY, TZ)
    assert [occ.slot.id for occ in found] == [1]


def test_slot_for_an_unknown_medicine_is_ignored():
    assert occurrences_for_date([make_slot(medication_id=99)], {}, ROUTINE, MONDAY, TZ) == []


@pytest.mark.parametrize(
    ("anchor", "offset", "expected"),
    [
        ("breakfast", 0, "With breakfast"),
        ("breakfast", 15, "15 min after breakfast"),
        ("dinner", -30, "30 min before dinner"),
        ("lunch", 60, "1 hour after lunch"),
        ("bedtime", 0, "At bedtime"),
        ("wake", 0, "On waking up"),
    ],
)
def test_describe_slot_uses_everyday_words(anchor, offset, expected):
    assert describe_slot(make_slot(anchor=anchor, offset_minutes=offset)) == expected


def test_describe_fixed_slot():
    assert describe_slot(make_slot(anchor="fixed", fixed_time=time(9, 0))) == "At a set time"


def test_doses_per_day_averages_over_the_week_and_ignores_retired_slots():
    slots = [
        make_slot(1),
        make_slot(2, days_mask="1000000"),
        make_slot(3, retired_at=local(2026, 11, 1)),
    ]
    assert doses_per_day(slots) == pytest.approx(1 + 1 / 7)
