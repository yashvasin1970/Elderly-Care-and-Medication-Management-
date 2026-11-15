"""Read a medicine described in a sentence into a structured draft.

"Amlodipine 5 mg every morning after breakfast for 30 days" becomes a name,
a dose, a time of day and a duration. The draft only ever pre-fills a form:
a person always checks it before anything is saved, and nothing here invents
or recommends a dose.

The same ``normalise_draft`` check is applied to the optional Gemini parser,
so AI output is held to exactly the same limits as the built-in rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import ANCHORS, FIXED

MAX_SLOTS = 4
MAX_NAME_LENGTH = 80
MAX_DOSE_LENGTH = 40
MAX_INSTRUCTIONS_LENGTH = 200
MAX_OFFSET_MINUTES = 120
MAX_DURATION_DAYS = 365


@dataclass(frozen=True, slots=True)
class SlotDraft:
    """A proposed time of day for a dose."""

    anchor: str
    offset_minutes: int = 0
    fixed_time: str | None = None  # "HH:MM", only when anchor is "fixed"


@dataclass(frozen=True, slots=True)
class MedicationDraft:
    """A medicine as understood from free text, awaiting human confirmation."""

    name: str
    dose: str
    slots: tuple[SlotDraft, ...]
    instructions: str = ""
    duration_days: int | None = None
    source: str = "rules"


_DOSE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(mg|mcg|µg|ug|g|ml|iu|units?|tablets?|tabs?|capsules?|caps?|puffs?|drops?)\b",
    re.IGNORECASE,
)
_DURATION = re.compile(r"\bfor\s+(\d{1,3})\s*(day|week|month)s?\b", re.IGNORECASE)
_CLOCK = re.compile(r"\bat\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_MEAL = re.compile(r"\b(before|after|with)\s+(breakfast|lunch|dinner|supper)\b", re.IGNORECASE)
_ALL_MEALS = re.compile(r"\b(before|after|with)\s+(?:meals|each meal|every meal)\b", re.IGNORECASE)
_BEDTIME = re.compile(
    r"\b(at bedtime|before bed|bedtime|at night|every night|nightly|before sleep(?:ing)?)\b",
    re.IGNORECASE,
)
_WAKING = re.compile(r"\b(on waking|after waking|when (?:i|you) wake(?: up)?|first thing)\b", re.I)
_PARTS_OF_DAY = (
    (re.compile(r"\bmornings?\b", re.IGNORECASE), "breakfast"),
    (re.compile(r"\bafternoons?\b", re.IGNORECASE), "lunch"),
    (re.compile(r"\bevenings?\b", re.IGNORECASE), "dinner"),
)
_FREQUENCIES = (
    (
        re.compile(r"\b(four times|4 times|4x|qid|qds)\b", re.I),
        ("wake", "lunch", "dinner", "bedtime"),
    ),
    (
        re.compile(r"\b(three times|3 times|3x|thrice|tid|tds)\b", re.I),
        ("breakfast", "lunch", "dinner"),
    ),
    (re.compile(r"\b(twice|two times|2 times|2x|bid|bd)\b", re.I), ("breakfast", "dinner")),
)
_INSTRUCTIONS = re.compile(
    r"\b(with food|with water|with milk|on an empty stomach|after meals|before meals)\b",
    re.IGNORECASE,
)
_LEADING_VERB = re.compile(r"^\s*(please\s+)?(add|take|start|give|new)\s+", re.IGNORECASE)
_NAME_STOP = re.compile(
    r"\b(every|each|once|twice|thrice|daily|two|three|four|before|after|with|at|for|in|on|\d)",
    re.IGNORECASE,
)
_MEAL_OFFSETS = {"before": -30, "with": 0, "after": 15}
_DAYS_PER_UNIT = {"day": 1, "week": 7, "month": 30}
_FIXED_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_medication_text(text: str) -> MedicationDraft:
    """Parse a free-text description using built-in rules."""
    text = " ".join(text.split())
    dose_match = _DOSE.search(text)
    return MedicationDraft(
        name=_extract_name(text, dose_match),
        dose=_format_dose(dose_match),
        slots=_extract_slots(text),
        instructions=_extract_instructions(text),
        duration_days=_extract_duration(text),
    )


def normalise_draft(data: Mapping[str, Any], source: str) -> MedicationDraft | None:
    """Validate untrusted draft data (for example AI output).

    Returns ``None`` if the essentials are missing, and silently drops any
    part that falls outside the limits the form itself enforces.
    """
    name = _clean(data.get("name"), MAX_NAME_LENGTH)
    if not name:
        return None
    slots: list[SlotDraft] = []
    raw_slots = data.get("slots")
    for raw in raw_slots if isinstance(raw_slots, list) else []:
        slot = _normalise_slot(raw)
        if slot is not None and slot not in slots:
            slots.append(slot)
    duration = data.get("duration_days")
    if not (isinstance(duration, int) and 1 <= duration <= MAX_DURATION_DAYS):
        duration = None
    return MedicationDraft(
        name=name,
        dose=_clean(data.get("dose"), MAX_DOSE_LENGTH),
        slots=tuple(slots[:MAX_SLOTS]) or (SlotDraft("breakfast"),),
        instructions=_clean(data.get("instructions"), MAX_INSTRUCTIONS_LENGTH),
        duration_days=duration,
        source=source,
    )


def _normalise_slot(raw: Any) -> SlotDraft | None:
    if not isinstance(raw, Mapping):
        return None
    anchor = raw.get("anchor")
    if anchor == FIXED:
        fixed_time = raw.get("fixed_time")
        if isinstance(fixed_time, str) and _FIXED_TIME.match(fixed_time):
            return SlotDraft(FIXED, 0, fixed_time)
        return None
    if anchor not in ANCHORS:
        return None
    offset = raw.get("offset_minutes", 0)
    if not isinstance(offset, int) or isinstance(offset, bool):
        offset = 0
    offset = max(-MAX_OFFSET_MINUTES, min(offset, MAX_OFFSET_MINUTES))
    return SlotDraft(anchor, offset)


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _extract_name(text: str, dose_match: re.Match[str] | None) -> str:
    candidate = text[: dose_match.start()] if dose_match and dose_match.start() > 0 else text
    candidate = _LEADING_VERB.sub("", candidate)
    stop = _NAME_STOP.search(candidate)
    if stop:
        candidate = candidate[: stop.start()]
    candidate = candidate.strip(" ,.;:-")
    if not candidate:
        return ""
    return (candidate[0].upper() + candidate[1:])[:MAX_NAME_LENGTH]


def _format_dose(dose_match: re.Match[str] | None) -> str:
    if dose_match is None:
        return ""
    amount = dose_match.group(1).replace(",", ".")
    unit = dose_match.group(2)
    unit = "IU" if unit.lower() == "iu" else unit.lower()
    return f"{amount} {unit}"


def _extract_slots(text: str) -> tuple[SlotDraft, ...]:
    slots: list[SlotDraft] = []

    def add(slot: SlotDraft) -> None:
        if slot not in slots:
            slots.append(slot)

    for relation, meal in _MEAL.findall(text):
        anchor = "dinner" if meal.lower() == "supper" else meal.lower()
        add(SlotDraft(anchor, _MEAL_OFFSETS[relation.lower()]))
    for hour, minute, meridiem in _CLOCK.findall(text):
        fixed = _to_24_hour(int(hour), int(minute or 0), meridiem.lower())
        if fixed:
            add(SlotDraft(FIXED, 0, fixed))
    if _BEDTIME.search(text):
        add(SlotDraft("bedtime"))
    if _WAKING.search(text):
        add(SlotDraft("wake"))

    if not slots:
        offset = 0
        all_meals = _ALL_MEALS.search(text)
        if all_meals:
            offset = _MEAL_OFFSETS[all_meals.group(1).lower()]
        anchors = _frequency_anchors(text)
        if anchors is None and all_meals:
            anchors = ("breakfast", "lunch", "dinner")
        if anchors is None:
            anchors = tuple(anchor for pattern, anchor in _PARTS_OF_DAY if pattern.search(text))
        for anchor in anchors or ("breakfast",):
            meal_offset = offset if anchor in ("breakfast", "lunch", "dinner") else 0
            add(SlotDraft(anchor, meal_offset))
    return tuple(slots[:MAX_SLOTS])


def _frequency_anchors(text: str) -> tuple[str, ...] | None:
    for pattern, anchors in _FREQUENCIES:
        if pattern.search(text):
            return anchors
    return None


def _to_24_hour(hour: int, minute: int, meridiem: str) -> str | None:
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return f"{hour:02d}:{minute:02d}"


def _extract_instructions(text: str) -> str:
    found: list[str] = []
    for phrase in _INSTRUCTIONS.findall(text):
        phrase = phrase.lower()
        if phrase not in found:
            found.append(phrase)
    return "; ".join(found).capitalize()


def _extract_duration(text: str) -> int | None:
    match = _DURATION.search(text)
    if match is None:
        return None
    days = int(match.group(1)) * _DAYS_PER_UNIT[match.group(2).lower()]
    return days if 1 <= days <= MAX_DURATION_DAYS else None
