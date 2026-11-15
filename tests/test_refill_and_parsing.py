"""Refill forecasts and reading a medicine from a sentence."""

from __future__ import annotations

from datetime import date

import pytest

from hearth.engine.parsing import (
    MAX_SLOTS,
    SlotDraft,
    normalise_draft,
    parse_medication_text,
)
from hearth.engine.refill import forecast_refill

from .factories import local, make_medication, make_slot

TODAY = date(2026, 11, 16)

# --- refill forecast ------------------------------------------------------


def test_untracked_stock_gives_no_forecast():
    assert forecast_refill(make_medication(), [make_slot()], TODAY) is None


def test_stock_running_low_needs_a_refill():
    medication = make_medication(stock_count=10)
    forecast = forecast_refill(medication, [make_slot(1), make_slot(2, anchor="dinner")], TODAY)
    assert forecast is not None
    assert forecast.days_left == 5
    assert forecast.runs_out_on == date(2026, 11, 21)
    assert forecast.needs_refill


def test_plenty_of_stock_needs_no_refill():
    forecast = forecast_refill(make_medication(stock_count=60), [make_slot()], TODAY)
    assert forecast is not None
    assert forecast.days_left == 60
    assert not forecast.needs_refill


def test_units_per_dose_use_up_stock_faster():
    medication = make_medication(stock_count=20, units_per_dose=2)
    forecast = forecast_refill(medication, [make_slot()], TODAY)
    assert forecast is not None and forecast.days_left == 10


def test_doses_on_some_days_only_last_longer():
    medication = make_medication(stock_count=4)
    weekly = make_slot(days_mask="1000000")
    forecast = forecast_refill(medication, [weekly], TODAY)
    assert forecast is not None and forecast.days_left == 28


def test_no_active_slots_gives_no_forecast():
    retired = make_slot(retired_at=local(2026, 11, 1))
    assert forecast_refill(make_medication(stock_count=10), [retired], TODAY) is None


# --- sentence parsing -----------------------------------------------------


def test_full_sentence_is_understood():
    draft = parse_medication_text("Amlodipine 5mg every morning after breakfast for 30 days")
    assert draft.name == "Amlodipine"
    assert draft.dose == "5 mg"
    assert draft.slots == (SlotDraft("breakfast", 15),)
    assert draft.duration_days == 30
    assert draft.source == "rules"


def test_leading_verb_is_not_part_of_the_name():
    assert parse_medication_text("Please add Metformin 500 mg twice daily").name == "Metformin"


def test_twice_daily_means_breakfast_and_dinner():
    draft = parse_medication_text("Metformin 500 mg twice daily with food")
    assert draft.slots == (SlotDraft("breakfast"), SlotDraft("dinner"))
    assert draft.instructions == "With food"


def test_three_times_a_day_after_meals():
    draft = parse_medication_text("Paracetamol 650 mg three times a day after meals")
    assert draft.slots == (
        SlotDraft("breakfast", 15),
        SlotDraft("lunch", 15),
        SlotDraft("dinner", 15),
    )


def test_exact_clock_time_becomes_a_fixed_slot():
    draft = parse_medication_text("Insulin 10 units at 9:30 pm")
    assert draft.dose == "10 units"
    assert draft.slots == (SlotDraft("fixed", 0, "21:30"),)


def test_bedtime_and_units_are_recognised():
    draft = parse_medication_text("Vitamin D3 1000 IU at bedtime for 8 weeks")
    assert draft.name == "Vitamin D3"
    assert draft.dose == "1000 IU"
    assert draft.slots == (SlotDraft("bedtime"),)
    assert draft.duration_days == 56


def test_morning_and_evening_map_to_meals():
    draft = parse_medication_text("Losartan 50 mg morning and evening")
    assert draft.slots == (SlotDraft("breakfast"), SlotDraft("dinner"))


def test_unclear_timing_defaults_to_breakfast_for_the_person_to_correct():
    draft = parse_medication_text("Aspirin 75 mg")
    assert draft.slots == (SlotDraft("breakfast"),)
    assert draft.duration_days is None


def test_missing_dose_is_left_blank_rather_than_guessed():
    draft = parse_medication_text("Thyroxine every morning")
    assert draft.name == "Thyroxine"
    assert draft.dose == ""


# --- validating untrusted (AI) drafts ------------------------------------


def test_valid_ai_draft_is_accepted():
    draft = normalise_draft(
        {
            "name": "  Amlodipine ",
            "dose": "5 mg",
            "slots": [{"anchor": "breakfast", "offset_minutes": 15}],
            "instructions": "With water",
            "duration_days": 30,
        },
        source="gemini",
    )
    assert draft is not None
    assert draft.name == "Amlodipine"
    assert draft.slots == (SlotDraft("breakfast", 15),)
    assert draft.source == "gemini"


def test_ai_draft_without_a_name_is_rejected():
    assert normalise_draft({"dose": "5 mg"}, source="gemini") is None
    assert normalise_draft({"name": 42}, source="gemini") is None


def test_invalid_parts_of_an_ai_draft_are_dropped_or_clamped():
    draft = normalise_draft(
        {
            "name": "X" * 500,
            "dose": None,
            "slots": [
                {"anchor": "midnight-snack"},
                {"anchor": "fixed", "fixed_time": "25:99"},
                {"anchor": "dinner", "offset_minutes": 9999},
                "not-a-slot",
            ],
            "duration_days": 100000,
        },
        source="gemini",
    )
    assert draft is not None
    assert len(draft.name) == 80
    assert draft.dose == ""
    assert draft.slots == (SlotDraft("dinner", 120),)
    assert draft.duration_days is None


@pytest.mark.parametrize("slots", [None, [], "breakfast"])
def test_ai_draft_with_no_usable_slots_falls_back_to_breakfast(slots):
    draft = normalise_draft({"name": "Aspirin", "slots": slots}, source="gemini")
    assert draft is not None and draft.slots == (SlotDraft("breakfast"),)


def test_ai_draft_cannot_exceed_the_slot_limit():
    slots = [{"anchor": "breakfast", "offset_minutes": offset} for offset in range(0, 70, 10)]
    draft = normalise_draft({"name": "Aspirin", "slots": slots}, source="gemini")
    assert draft is not None and len(draft.slots) == MAX_SLOTS
