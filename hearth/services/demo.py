"""A ready-made example circle, so Hearth can be explored straight away.

The people, medicines and history here are entirely fictional. The data is
generated relative to "now", and the last day is replayed through the real
reminder job, so the Outbox shows genuine reminders rather than canned ones.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from ..access import Role
from ..db import transaction
from ..engine.models import DoseStatus, Occurrence
from ..repositories import appointments, doses, medications, profiles, records
from ..repositories.appointments import AppointmentInput
from ..repositories.medications import MedicationInput, SlotInput
from . import care, passcodes, reminders

DEMO_CIRCLE_CODE = "DEMO2026"
DEMO_BASE_URL = "http://localhost:5000"


@dataclass(frozen=True, slots=True)
class DemoPerson:
    """Someone in the example circle, as offered on the sign-in page."""

    name: str
    passcode: str
    view: str
    about: str


#: Demo sign-in details, also listed in the README. Never use these for real data.
DEMO_PEOPLE: tuple[DemoPerson, ...] = (
    DemoPerson(
        "Kamala Rao",
        "sunrise-2468",
        "Senior view",
        "The elder who owns the circle: large, simple screens and an “I need help” button.",
    ),
    DemoPerson(
        "Joseph",
        "caring-1357",
        "Caretaker dashboard",
        "Records doses and sees the whole day, with demo tools to test alerts.",
    ),
    DemoPerson(
        "Priya",
        "family-8642",
        "Family overview",
        "Follows from afar. Her changes wait for Kamala’s approval.",
    ),
    DemoPerson("Dr. Mehta", "doctor-9753", "Read-only view", "Can look, but not change anything."),
)
DEMO_PASSCODES = {person.name: person.passcode for person in DEMO_PEOPLE}

_HISTORY_DAYS = 35
_REPLAY_HOURS = 30
_REPLAY_STEP = timedelta(minutes=10)
#: Doses due more recently than this are left open, so there is something to do.
_LEAVE_OPEN = timedelta(hours=2)
_LAST_VISIT_DAYS_AGO = 9

_EVERY_DAY = "1111111"


def seed_if_empty(db: sqlite3.Connection, now: datetime, hash_method: str) -> bool:
    """Create the demo circle when the database has no circles at all."""
    if profiles.count_profiles(db) > 0:
        return False
    _seed(db, now, hash_method)
    return True


def seed_if_missing(db: sqlite3.Connection, now: datetime, hash_method: str) -> bool:
    """Create the demo circle unless it already exists."""
    if profiles.get_profile_by_code(db, DEMO_CIRCLE_CODE) is not None:
        return False
    _seed(db, now, hash_method)
    return True


def _seed(db: sqlite3.Connection, now: datetime, hash_method: str) -> None:
    started = now - timedelta(days=_HISTORY_DAYS)
    with transaction(db):
        profile_id = profiles.create_profile(
            db,
            circle_code=DEMO_CIRCLE_CODE,
            elder_name="Kamala Rao",
            timezone="Asia/Kolkata",
            now=started,
        )
        people = _add_people(db, profile_id, started, hash_method)
        _add_medicines(db, profile_id, started)
        profile = profiles.get_profile(db, profile_id)
        if profile is None:  # pragma: no cover - the row was inserted just above
            raise RuntimeError("The demo profile could not be created.")
        ctx = care.load_context(db, profile)
        _add_appointments(db, ctx, people, now)
        _add_journal(db, profile_id, people, now)
        recent = _add_history(db, ctx, people, now)
        _add_pending_request(db, profile_id, people, now)

    # Replay the last day so the Outbox fills with what really would have gone out.
    _replay(db, profile_id, recent, now)


def _add_people(
    db: sqlite3.Connection, profile_id: int, now: datetime, hash_method: str
) -> dict[str, int]:
    people = [
        ("Kamala Rao", "kamala@example.com", Role.ELDER, False, True),
        ("Joseph", "joseph@example.com", Role.CARETAKER, True, True),
        ("Priya", "priya@example.com", Role.FAMILY, False, True),
        ("Dr. Mehta", "", Role.VIEWER, False, False),
    ]
    ids = {}
    for name, email, role, trusted, alerts in people:
        ids[name] = profiles.add_member(
            db,
            profile_id=profile_id,
            name=name,
            email=email,
            role=role,
            passcode_hash=passcodes.hash_passcode(DEMO_PASSCODES[name], hash_method),
            trusted_editor=trusted,
            receives_alerts=alerts,
            now=now,
        )
    records.add_audit(
        db, profile_id=profile_id, member_id=ids["Kamala Rao"], action="Created the circle", now=now
    )
    for name in ("Joseph", "Priya", "Dr. Mehta"):
        records.add_audit(
            db,
            profile_id=profile_id,
            member_id=ids["Kamala Rao"],
            action="Added someone to the circle",
            detail=name,
            now=now,
        )
    return ids


def _medicine(
    name: str, dose: str, criticality: str, slots: tuple[SlotInput, ...], **extra: object
) -> MedicationInput:
    values: dict[str, object] = {
        "name": name,
        "dose": dose,
        "criticality": criticality,
        "instructions": "",
        "missed_dose_rule": "",
        "pill_color": "white",
        "pill_shape": "round",
        "stock_count": None,
        "units_per_dose": 1,
        "end_date": None,
        "days_mask": _EVERY_DAY,
        "slots": slots,
    }
    values.update(extra)
    return MedicationInput(**values)  # type: ignore[arg-type]


def _add_medicines(db: sqlite3.Connection, profile_id: int, started: datetime) -> None:
    start = started.date().isoformat()
    medicines = [
        _medicine(
            "Metformin",
            "500 mg",
            "important",
            (SlotInput("breakfast", 15), SlotInput("dinner", 15)),
            instructions="With food",
            missed_dose_rule=(
                "If it is nearly time for the next dose, skip the missed one."
                " Never take two together."
            ),
            pill_shape="oval",
            stock_count=46,
            start_date=start,
        ),
        _medicine(
            "Amlodipine",
            "5 mg",
            "important",
            (SlotInput("breakfast", 0),),
            stock_count=22,
            start_date=start,
        ),
        _medicine(
            "Insulin glargine",
            "10 units",
            "critical",
            (SlotInput("bedtime", 0),),
            missed_dose_rule="Call the diabetes nurse before taking a late dose.",
            pill_color="blue",
            pill_shape="injection",
            start_date=start,
        ),
        _medicine(
            "Atorvastatin",
            "10 mg",
            "routine",
            (SlotInput("bedtime", 0),),
            pill_color="pink",
            pill_shape="oval",
            stock_count=30,
            start_date=start,
        ),
        _medicine(
            "Vitamin D3",
            "1000 IU",
            "routine",
            (SlotInput("lunch", 0),),
            pill_color="yellow",
            pill_shape="capsule",
            stock_count=4,
            start_date=start,
        ),
    ]
    for medicine in medicines:
        medications.create_medication(db, profile_id, medicine, started)


def _add_appointments(
    db: sqlite3.Connection, ctx: care.CareContext, people: dict[str, int], now: datetime
) -> None:
    today = ctx.local(now).date()

    def at(day_offset: int, hour: int, minute: int) -> datetime:
        return datetime.combine(
            today + timedelta(days=day_offset), time(hour, minute), tzinfo=ctx.tz
        )

    visits = [
        AppointmentInput(
            "Diabetes review",
            "Dr. Mehta",
            "City Clinic, Room 4",
            at(-_LAST_VISIT_DAYS_AGO, 10, 30),
            "",
        ),
        AppointmentInput(
            "Diabetes follow-up",
            "Dr. Mehta",
            "City Clinic, Room 4",
            at(1, 10, 30),
            "Bring the blood sugar diary.",
        ),
        AppointmentInput("Eye check-up", "Dr. Fernandes", "Vision Centre", at(12, 16, 0), ""),
    ]
    for visit in visits:
        appointments.create_appointment(
            db, ctx.profile.id, visit, created_by=people["Priya"], now=now - timedelta(days=14)
        )


def _add_journal(
    db: sqlite3.Connection, profile_id: int, people: dict[str, int], now: datetime
) -> None:
    entries = [
        (
            6,
            "Kamala Rao",
            "symptom",
            "Felt dizzy after the evening walk. Sat down for ten minutes.",
        ),
        (
            4,
            "Kamala Rao",
            "question",
            "Can I take the vitamin D with my morning tea instead of at lunch?",
        ),
        (2, "Joseph", "note", "Blood pressure 132/84 this morning. Good appetite all week."),
        (
            1,
            "Priya",
            "question",
            "Should the evening Metformin move earlier on physiotherapy days?",
        ),
    ]
    for days_ago, author, kind, body in entries:
        records.add_journal_entry(
            db,
            profile_id=profile_id,
            member_id=people[author],
            kind=kind,
            body=body,
            now=now - timedelta(days=days_ago, hours=3),
        )


# --- dose history ---------------------------------------------------------

_Log = tuple[Occurrence, DoseStatus, datetime, int]


def _add_history(
    db: sqlite3.Connection, ctx: care.CareContext, people: dict[str, int], now: datetime
) -> list[_Log]:
    """Write four weeks of dose history.

    Returns the most recent entries unsaved, so they can be replayed in order
    alongside the reminder job.
    """
    rng = random.Random(2026)  # noqa: S311 - fixed seed for repeatable demo data, not security
    local_now = ctx.local(now)
    today = local_now.date()
    replay_from = local_now - timedelta(hours=_REPLAY_HOURS)
    recent: list[_Log] = []

    for days_ago in range(28, -1, -1):
        day = today - timedelta(days=days_ago)
        for occ in ctx.occurrences(day):
            if occ.scheduled_at > local_now - _LEAVE_OPEN:
                continue
            outcome = _outcome(occ, days_ago, day, rng)
            if outcome is None:
                continue  # a missed dose: nothing was recorded
            status, minutes_late = outcome
            acted_at = occ.scheduled_at + timedelta(minutes=minutes_late)
            actor = people["Joseph"] if occ.slot.anchor == "dinner" else people["Kamala Rao"]
            if days_ago == 1 and occ.medication.criticality == "critical":
                # Last night's insulin was late, and Joseph stepped in after the alert.
                acted_at, actor = occ.scheduled_at + timedelta(minutes=72), people["Joseph"]
            entry = (occ, status, min(acted_at, local_now), actor)
            if occ.scheduled_at >= replay_from:
                recent.append(entry)
            else:
                _save(db, ctx.profile.id, entry)
    return sorted(recent, key=lambda entry: entry[2])


def _outcome(
    occ: Occurrence, days_ago: int, day: date, rng: random.Random
) -> tuple[DoseStatus, int] | None:
    """Decide how a past dose went. The patterns here are what Care Weather finds."""
    name, anchor = occ.medication.name, occ.slot.anchor
    if days_ago == _LAST_VISIT_DAYS_AGO and (name, anchor) in {
        ("Amlodipine", "breakfast"),
        ("Metformin", "dinner"),
    }:
        return None  # doses slip on the day of a doctor visit
    if anchor == "dinner" and day.weekday() == 1:
        return None  # Tuesday is physiotherapy day: the evening dose gets missed
    if name == "Vitamin D3" and days_ago in (5, 11, 20):
        return None
    if name == "Atorvastatin" and days_ago in (3, 8, 15):
        return DoseStatus.SKIPPED, rng.randint(0, 10)
    minutes_late = {
        "breakfast": rng.randint(32, 48),  # breakfast really happens around 8:40
        "lunch": rng.randint(0, 20),
        "dinner": rng.randint(5, 25),
        "bedtime": rng.randint(0, 15),
    }[anchor]
    return DoseStatus.TAKEN, minutes_late


def _save(db: sqlite3.Connection, profile_id: int, entry: _Log) -> None:
    occ, status, acted_at, actor = entry
    doses.save_log(
        db,
        profile_id=profile_id,
        slot_id=occ.slot.id,
        dose_date=occ.dose_date,
        status=status,
        acted_at=acted_at,
        acted_by=actor,
    )


def _add_pending_request(
    db: sqlite3.Connection, profile_id: int, people: dict[str, int], now: datetime
) -> None:
    """Priya suggests moving a medicine: it waits for Kamala's decision."""
    statin = next(
        med for med in medications.list_medications(db, profile_id) if med.name == "Atorvastatin"
    )
    current = medications.get_input(db, profile_id, statin.id)
    if current is None:  # pragma: no cover - the medicine was created just above
        return
    proposed = replace(current, slots=(SlotInput("dinner", 0),))
    moment = now - timedelta(hours=2)
    medications.create_request(
        db,
        profile_id=profile_id,
        medication_id=statin.id,
        action="update",
        payload=proposed,
        reason="Mum often skips it at bedtime. Dr. Mehta said taking it with dinner is fine.",
        proposed_by=people["Priya"],
        now=moment,
    )
    records.add_audit(
        db,
        profile_id=profile_id,
        member_id=people["Priya"],
        action="Suggested a change",
        detail="Change Atorvastatin",
        now=moment,
    )


# --- replaying the last day ----------------------------------------------


def _replay(db: sqlite3.Connection, profile_id: int, recent: list[_Log], now: datetime) -> None:
    """Step through the last day, logging doses and running reminders in order."""
    profile = profiles.get_profile(db, profile_id)
    if profile is None:  # pragma: no cover - the profile was created just above
        return
    ctx = care.load_context(db, profile)

    local_now = ctx.local(now)
    moment = now - timedelta(hours=_REPLAY_HOURS)

    # The weekly summary goes out on Sunday evening. If the last one falls
    # before the replay window, run that moment once so a summary is on show.
    days_since_sunday = (local_now.weekday() - reminders.DIGEST_WEEKDAY) % 7
    sunday_evening = datetime.combine(
        local_now.date() - timedelta(days=days_since_sunday),
        time(reminders.DIGEST_HOUR, 5),
        tzinfo=ctx.tz,
    )
    if sunday_evening > local_now:
        sunday_evening -= timedelta(days=7)
    if sunday_evening < moment:
        reminders.run_profile(db, None, profile, sunday_evening, DEMO_BASE_URL)

    pending = list(recent)
    while moment <= now:
        with transaction(db):
            while pending and pending[0][2] <= moment:
                _save(db, profile_id, pending.pop(0))
            reminders.run_profile(db, None, profile, moment, DEMO_BASE_URL)
        moment += _REPLAY_STEP
    with transaction(db):
        for entry in pending:
            _save(db, profile_id, entry)
        reminders.run_profile(db, None, profile, now, DEMO_BASE_URL)
