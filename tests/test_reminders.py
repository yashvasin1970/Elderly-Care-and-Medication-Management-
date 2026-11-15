"""The reminder job: what gets sent, to whom, and never twice."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hearth.access import Role
from hearth.db import get_db
from hearth.engine.models import DoseStatus
from hearth.repositories import appointments, doses, profiles, records
from hearth.repositories.appointments import AppointmentInput
from hearth.repositories.medications import SlotInput
from hearth.services import care, consent, reminders
from hearth.services.mailer import SmtpMailer, deliver, mailer_from_config

from .conftest import START, add_medicine, medicine

KOLKATA = ZoneInfo("Asia/Kolkata")
EIGHT_PM = (SlotInput("fixed", 0, "20:00"),)
ELDER, CARER, FAMILY = "kamala@example.com", "joseph@example.com", "priya@example.com"


def ist(hour: int, minute: int = 0, day: int = 16) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=KOLKATA)


class FakeMailer:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail

    def send(self, recipient: str, subject: str, body: str) -> None:
        if self.fail:
            raise OSError("mail server unreachable")
        self.sent.append((recipient, subject, body))


@pytest.fixture
def run(app, circle):
    def run_at(now: datetime, mailer=None) -> int:
        with app.app_context():
            return reminders.run(get_db(), mailer, now, "https://hearth.example")

    return run_at


@pytest.fixture
def sent(app, circle):
    """Return the outbox as (kind, recipient) pairs, oldest first."""

    def collect(kind: str | None = None) -> list[tuple[str, str]]:
        with app.app_context():
            notes = records.list_notifications(get_db(), circle.profile_id)
        pairs = [(n.kind, n.recipient) for n in reversed(notes)]
        return [p for p in pairs if kind is None or p[0] == kind]

    return collect


def add(app, circle, *args, **kwargs):
    add_medicine(app, circle, medicine(*args, **kwargs), when=START - timedelta(days=1))


# --- the dose ladder ------------------------------------------------------


def test_critical_dose_climbs_the_ladder_one_step_at_a_time(app, circle, run, sent):
    add(app, circle, "Insulin", "critical", slots=EIGHT_PM)

    run(ist(19, 52))
    assert sent("upcoming") == [("upcoming", ELDER)]
    run(ist(20, 21))
    assert sent("nudge") == [("nudge", ELDER)]
    run(ist(20, 31))
    assert sent("missed") == [("missed", CARER)]
    run(ist(21, 1))
    assert sent("missed") == [("missed", CARER), ("missed", FAMILY)]


def test_running_the_job_again_never_sends_twice(app, circle, run, sent):
    add(app, circle, "Insulin", "critical", slots=EIGHT_PM)
    assert run(ist(20, 31)) > 0
    before = sent()
    assert run(ist(20, 32)) == 0
    assert run(ist(20, 33)) == 0
    assert sent() == before


def test_routine_medicine_only_ever_reminds_the_elder(app, circle, run, sent):
    add(app, circle, "Vitamin D", "routine", slots=EIGHT_PM)
    for minute in range(0, 180, 10):
        run(ist(20) + timedelta(minutes=minute))
    assert sent("missed") == []
    assert sent("nudge") == [("nudge", ELDER)]


def test_a_taken_dose_raises_no_alarm(app, circle, run, sent):
    add(app, circle, "Insulin", "critical", slots=EIGHT_PM)
    with app.app_context():
        doses.save_log(
            get_db(),
            profile_id=circle.profile_id,
            slot_id=1,
            dose_date=ist(20).date(),
            status=DoseStatus.TAKEN,
            acted_at=ist(20, 5),
            acted_by=circle.members[Role.ELDER],
        )
    run(ist(20, 31))
    run(ist(21, 1))
    assert sent("missed") == []
    assert sent("nudge") == []


def test_without_a_caretaker_the_family_is_told_once(app, circle, run, sent):
    add(app, circle, "Insulin", "critical", slots=EIGHT_PM)
    with app.app_context():
        profiles.deactivate_member(get_db(), circle.profile_id, circle.members[Role.CARETAKER])
    run(ist(20, 31))
    run(ist(21, 1))
    assert sent("missed") == [("missed", FAMILY)]


def test_doses_due_together_share_one_email(app, circle, run):
    add(app, circle, "Insulin", "critical", slots=EIGHT_PM)
    add(app, circle, "Atorvastatin", "routine", slots=EIGHT_PM)
    mailer = FakeMailer()
    run(ist(19, 55), mailer)
    [(recipient, subject, body)] = mailer.sent
    assert recipient == ELDER
    assert subject == "Coming up at 8:00 PM: Atorvastatin, Insulin"
    assert "- Insulin 500 mg" in body and "- Atorvastatin 500 mg" in body


def test_snoozing_moves_the_nudge_and_the_alerts(app, circle, run, sent):
    add(app, circle, "Insulin", "critical", slots=EIGHT_PM)
    with app.app_context():
        db = get_db()
        profile = profiles.get_profile(db, circle.profile_id)
        elder = profiles.get_member(db, circle.profile_id, circle.members[Role.ELDER])
        assert profile is not None and elder is not None
        care.record_dose(
            db,
            care.load_context(db, profile),
            elder,
            slot_id=1,
            dose_date=ist(20).date(),
            action="snooze",
            now=ist(20, 5),
        )
    run(ist(20, 19))
    assert sent("nudge") == []
    run(ist(20, 21))
    assert sent("nudge") == [("nudge", ELDER)]
    run(ist(20, 31))
    assert sent("missed") == []  # the ladder restarted from the end of the snooze
    run(ist(20, 51))
    assert sent("missed") == [("missed", CARER)]


def test_missed_alert_quotes_the_labels_advice(app, circle, run):
    add(
        app,
        circle,
        "Insulin",
        "critical",
        slots=EIGHT_PM,
        missed_dose_rule="Call the nurse before a late dose.",
    )
    mailer = FakeMailer()
    run(ist(20, 31), mailer)
    alert = next(body for recipient, _, body in mailer.sent if recipient == CARER)
    assert "Call the nurse before a late dose." in alert
    assert "Kamala was reminded first" in alert


# --- appointments, refills, requests -------------------------------------


def test_appointment_reminders_go_out_the_day_before_and_on_the_day(app, circle, run, sent):
    with app.app_context():
        appointments.create_appointment(
            get_db(),
            circle.profile_id,
            AppointmentInput("Eye test", "Dr. Fernandes", "Vision Centre", ist(10, 30, day=17), ""),
            created_by=circle.members[Role.FAMILY],
            now=START,
        )
    run(ist(9, 0))
    assert sent("appointment") == []
    run(ist(10, 31))
    assert sorted(sent("appointment")) == sorted(
        [("appointment", ELDER), ("appointment", CARER), ("appointment", FAMILY)]
    )
    run(ist(8, 31, day=17))
    assert len(sent("appointment")) == 6
    run(ist(10, 31, day=17))
    assert len(sent("appointment")) == 6


def test_refill_reminder_is_sent_once_a_week(app, circle, run, sent):
    add(app, circle, "Vitamin D", "routine", stock_count=3)
    run(ist(9, 0))
    assert len(sent("refill")) == 3
    run(ist(9, 0, day=18))
    assert len(sent("refill")) == 3
    run(ist(9, 0, day=23))  # a new week
    assert len(sent("refill")) == 6


def test_the_elder_hears_about_suggested_changes(app, circle, run):
    with app.app_context():
        db = get_db()
        family = profiles.get_member(db, circle.profile_id, circle.members[Role.FAMILY])
        assert family is not None
        consent.submit_change(
            db,
            family,
            action="create",
            medication_id=None,
            payload=medicine("Losartan"),
            reason="New prescription",
            now=ist(9, 0),
        )
    mailer = FakeMailer()
    run(ist(8, 0), mailer)  # before the suggestion existed: nothing to say
    assert mailer.sent == []
    run(ist(9, 5), mailer)
    [(recipient, subject, body)] = mailer.sent
    assert recipient == ELDER
    assert subject == "Priya suggested a change to your medicines"
    assert "Nothing changes unless you approve it" in body


# --- quiet check-in -------------------------------------------------------


def test_quiet_checkin_tells_supporters_after_the_chosen_hour(app, circle, run, sent):
    add(app, circle)
    run(ist(11, 59))
    assert sent("checkin") == []
    run(ist(12, 30))
    assert sorted(sent("checkin")) == [("checkin", CARER), ("checkin", FAMILY)]


def test_no_checkin_when_the_elder_has_been_active(app, circle, run, sent):
    add(app, circle)
    with app.app_context():
        profiles.touch_elder_activity(get_db(), circle.profile_id, ist(9, 0))
    run(ist(12, 30))
    assert sent("checkin") == []


def test_no_checkin_when_the_elder_switched_it_off(app, circle, run, sent):
    add(app, circle)
    with app.app_context():
        profiles.update_settings(
            get_db(),
            circle.profile_id,
            elder_name="Kamala",
            timezone="Asia/Kolkata",
            checkin_enabled=False,
            checkin_hour=12,
        )
    run(ist(12, 30))
    assert sent("checkin") == []


# --- the weekly summary ---------------------------------------------------


def test_weekly_care_weather_goes_to_everyone_on_sunday_evening(app, circle, run, sent):
    add(app, circle)
    run(ist(17, 59, day=22))
    assert sent("digest") == []
    run(ist(18, 5, day=22))
    run(ist(19, 0, day=22))
    assert sorted(sent("digest")) == sorted(
        [("digest", ELDER), ("digest", CARER), ("digest", FAMILY)]
    )


# --- delivery -------------------------------------------------------------


def test_a_mail_failure_is_recorded_and_does_not_stop_the_run(app, circle, run):
    add(app, circle, "Insulin", "critical", slots=EIGHT_PM)
    assert run(ist(20, 31), FakeMailer(fail=True)) > 0
    with app.app_context():
        notes = records.list_notifications(get_db(), circle.profile_id)
    assert notes and all(n.status == "failed" for n in notes)
    assert "unreachable" in notes[0].error


def test_without_smtp_settings_messages_go_to_the_outbox(app, circle):
    assert mailer_from_config({"SMTP_HOST": None}) is None
    with app.app_context():
        created = deliver(
            get_db(),
            None,
            profile_id=circle.profile_id,
            dedupe_key="k",
            recipient=ELDER,
            kind="test",
            subject="Hello",
            body="Body",
            now=START,
        )
        again = deliver(
            get_db(),
            None,
            profile_id=circle.profile_id,
            dedupe_key="k",
            recipient=ELDER,
            kind="test",
            subject="Hello",
            body="Body",
            now=START,
        )
        [note] = records.list_notifications(get_db(), circle.profile_id)
    assert created and not again
    assert note.status == "outbox"


def test_smtp_settings_build_a_real_mailer():
    mailer = mailer_from_config(
        {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": 2525,
            "SMTP_USERNAME": "user@example.com",
            "SMTP_PASSWORD": "secret",
            "SMTP_USE_TLS": True,
        }
    )
    assert isinstance(mailer, SmtpMailer)
    assert mailer.port == 2525
    assert mailer.sender == "user@example.com"
