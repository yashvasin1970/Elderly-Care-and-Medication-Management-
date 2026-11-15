"""End-to-end flows through the pages, as each kind of circle member."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hearth.access import Role
from hearth.db import get_db
from hearth.engine.models import DoseStatus
from hearth.repositories import doses, medications, profiles

from .conftest import START, Browser, add_medicine, medicine

KOLKATA = ZoneInfo("Asia/Kolkata")


def medicine_form(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Losartan",
        "dose": "50 mg",
        "criticality": "important",
        "units_per_dose": "1",
        "start_date": "2026-11-16",
        "slot-0-anchor": "breakfast",
        "slot-0-offset": "0",
        "pill_color": "white",
        "pill_shape": "round",
    }
    data.update({f"day-{index}": "1" for index in range(7)})
    data.update(overrides)
    return data


def take_action(page: str, action: str = "take") -> str:
    match = re.search(rf"/doses/\d+/\d{{4}}-\d{{2}}-\d{{2}}/{action}", page)
    assert match is not None, f"no {action} button on the page"
    return match.group(0)


# --- creating a circle ----------------------------------------------------


def test_creating_a_circle_makes_the_elder_its_owner(browser: Browser):
    response = browser.post(
        "/start",
        {
            "elder_name": "Asha",
            "email": "asha@example.com",
            "timezone": "Asia/Kolkata",
            "passcode": "my-own-pass",
            "passcode_again": "my-own-pass",
        },
    )
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Your circle is ready, Asha" in page
    assert re.search(r"[A-Z2-9]{4}-[A-Z2-9]{4}", page)
    assert "(circle owner)" in browser.get("/today").get_data(as_text=True)


def test_creating_a_circle_explains_every_problem(browser: Browser):
    response = browser.post(
        "/start",
        {"elder_name": "", "email": "nope", "timezone": "Mars/Base", "passcode": "abc"},
    )
    page = response.get_data(as_text=True)
    assert response.status_code == 422
    assert "Enter the name of the person" in page
    assert "email address does not look right" in page
    assert "Choose a timezone" in page
    assert "at least 6 characters" in page


# --- medicines and the elder's consent -----------------------------------


def test_the_elders_own_changes_apply_at_once(as_role):
    elder = as_role(Role.ELDER)
    response = elder.post("/medicines/new", medicine_form(), follow_redirects=True)
    assert "Losartan has been added" in response.get_data(as_text=True)
    assert "Losartan" in elder.get("/medicines").get_data(as_text=True)


def test_family_changes_wait_for_the_elder_to_approve(as_role):
    elder, family = as_role(Role.ELDER), as_role(Role.FAMILY)
    response = family.post(
        "/medicines/new", medicine_form(reason="Prescribed today"), follow_redirects=True
    )
    assert "Nothing changes until they approve it" in response.get_data(as_text=True)
    assert "Losartan" not in elder.get("/medicines").get_data(as_text=True)

    requests_page = elder.get("/requests").get_data(as_text=True)
    assert "Priya suggests: Add Losartan 50 mg" in requests_page
    assert "Prescribed today" in requests_page
    assert "1 waiting" in elder.get("/today").get_data(as_text=True)

    assert family.post("/requests/1/approve").status_code == 403
    elder.post("/requests/1/approve")
    assert "Losartan" in elder.get("/medicines").get_data(as_text=True)
    again = elder.post("/requests/1/approve", follow_redirects=True)
    assert "already been decided" in again.get_data(as_text=True)


def test_a_declined_suggestion_changes_nothing(as_role):
    elder, family = as_role(Role.ELDER), as_role(Role.FAMILY)
    family.post("/medicines/new", medicine_form())
    elder.post("/requests/1/decline")
    assert "Losartan" not in elder.get("/medicines").get_data(as_text=True)
    assert "Declined" in elder.get("/requests").get_data(as_text=True)


def test_someone_the_elder_trusts_can_change_medicines_directly(as_role, circle):
    elder, carer = as_role(Role.ELDER), as_role(Role.CARETAKER)
    elder.post(
        f"/circle/{circle.members[Role.CARETAKER]}/edit",
        {
            "name": "Joseph",
            "email": "joseph@example.com",
            "role": "caretaker",
            "trusted_editor": "1",
            "receives_alerts": "1",
        },
    )
    response = carer.post("/medicines/new", medicine_form(), follow_redirects=True)
    assert "Losartan has been added" in response.get_data(as_text=True)
    history = elder.get("/history").get_data(as_text=True)
    assert "Joseph" in history and "Added a medicine" in history


def test_invalid_medicine_form_explains_the_problem(as_role):
    response = as_role(Role.ELDER).post(
        "/medicines/new",
        medicine_form(
            name="",
            **{
                "slot-0-anchor": "",
                "day-0": "",
                "day-1": "",
                "day-2": "",
                "day-3": "",
                "day-4": "",
                "day-5": "",
                "day-6": "",
            },
        ),
    )
    page = response.get_data(as_text=True)
    assert response.status_code == 422
    assert "Enter the name of the medicine" in page
    assert "Choose at least one time of day" in page
    assert "Choose at least one day" in page


def test_a_sentence_prefills_the_form_without_saving(as_role):
    elder = as_role(Role.ELDER)
    response = elder.post(
        "/medicines/draft", {"sentence": "Amlodipine 5mg every morning after breakfast for 30 days"}
    )
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'value="Amlodipine"' in page
    assert 'value="5 mg"' in page
    assert 'value="2026-12-15"' in page  # 30 days, counting today
    assert "Nothing is saved yet" in page
    assert "No medicines yet" in elder.get("/medicines").get_data(as_text=True)


def test_stopping_a_medicine_keeps_its_history(app, as_role, circle):
    add_medicine(app, circle, medicine("Atorvastatin"), when=START - timedelta(days=2))
    elder = as_role(Role.ELDER)
    elder.post("/medicines/1/stop")
    page = elder.get("/medicines").get_data(as_text=True)
    assert "Stopped medicines (1)" in page
    assert "Atorvastatin" not in elder.get("/today").get_data(as_text=True)


# --- recording doses ------------------------------------------------------


def test_recording_a_dose_updates_status_and_stock(app, as_role, circle):
    add_medicine(app, circle, medicine(stock_count=10), when=START - timedelta(days=2))
    elder = as_role(Role.ELDER)
    page = elder.get("/today").get_data(as_text=True)
    assert "Late" in page  # 08:00 dose, now 09:00

    response = elder.post(take_action(page), follow_redirects=True)
    page = response.get_data(as_text=True)
    assert "Metformin is recorded as taken" in page
    assert "Taken at 9:00 AM by Kamala" in page
    with app.app_context():
        assert medications.get_medication(get_db(), circle.profile_id, 1).stock_count == 9

    elder.post(take_action(page, "undo"))
    with app.app_context():
        assert medications.get_medication(get_db(), circle.profile_id, 1).stock_count == 10


def test_snoozing_brings_the_dose_back_later(app, as_role, circle, clock):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=2))
    carer = as_role(Role.CARETAKER)
    page = carer.post(
        take_action(carer.get("/today").get_data(as_text=True), "snooze"), follow_redirects=True
    ).get_data(as_text=True)
    assert "will come up again in 15 minutes" in page
    assert "Reminder set" in page
    clock.advance(minutes=16)
    assert "Reminder set" not in carer.get("/today").get_data(as_text=True)


def test_skipping_is_recorded_as_the_elders_choice(app, as_role, circle):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=2))
    elder = as_role(Role.ELDER)
    page = elder.post(
        take_action(elder.get("/today").get_data(as_text=True), "skip"), follow_redirects=True
    ).get_data(as_text=True)
    assert "That is your decision to make" in page
    with app.app_context():
        log = doses.get_log(get_db(), circle.profile_id, 1, START.astimezone(KOLKATA).date())
    assert log is not None and log.status is DoseStatus.SKIPPED


def test_old_doses_cannot_be_rewritten(app, as_role, circle):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=10))
    response = as_role(Role.ELDER).post("/doses/1/2026-11-10/take", follow_redirects=True)
    assert "Only doses from today or yesterday" in response.get_data(as_text=True)


def test_late_dose_shows_the_labels_own_advice(app, as_role, circle):
    rule = "Skip it if the next dose is within 4 hours."
    add_medicine(app, circle, medicine(missed_dose_rule=rule), when=START - timedelta(days=2))
    assert rule in as_role(Role.ELDER).get("/today").get_data(as_text=True)


# --- learning the routine -------------------------------------------------


def test_accepting_a_routine_suggestion_moves_the_reminders(app, as_role, circle):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=10))
    with app.app_context():
        db = get_db()
        for day in range(10, 16):
            taken_at = datetime(2026, 11, day, 8, 40, tzinfo=KOLKATA)
            doses.save_log(
                db,
                profile_id=circle.profile_id,
                slot_id=1,
                dose_date=taken_at.date(),
                status=DoseStatus.TAKEN,
                acted_at=taken_at,
                acted_by=circle.members[Role.ELDER],
            )
    elder = as_role(Role.ELDER)
    page = elder.get("/today").get_data(as_text=True)
    assert "Yes, move to 8:40 AM" in page

    elder.post("/suggestions", {"key": "rhythm:breakfast:0840", "decision": "apply"})
    with app.app_context():
        profile = profiles.get_profile(get_db(), circle.profile_id)
    assert profile is not None and profile.routine["breakfast"].strftime("%H:%M") == "08:40"
    assert "Yes, move to" not in elder.get("/today").get_data(as_text=True)


def test_family_cannot_change_the_routine(as_role):
    family = as_role(Role.FAMILY)
    assert family.get("/routine").status_code == 200
    response = family.post(
        "/routine",
        {
            "wake": "07:00",
            "breakfast": "08:00",
            "lunch": "13:00",
            "dinner": "19:00",
            "bedtime": "22:00",
        },
    )
    assert response.status_code == 403


def test_routine_times_must_run_in_order(as_role):
    response = as_role(Role.ELDER).post(
        "/routine",
        {
            "wake": "09:00",
            "breakfast": "08:00",
            "lunch": "13:00",
            "dinner": "19:00",
            "bedtime": "22:00",
        },
    )
    assert response.status_code == 422
    assert "Times should run in order" in response.get_data(as_text=True)


# --- appointments and the visit summary ----------------------------------


def test_visit_summary_collects_questions_and_adherence(app, as_role, circle):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=5))
    elder, family = as_role(Role.ELDER), as_role(Role.FAMILY)
    family.post(
        "/appointments/new",
        {"title": "Diabetes review", "doctor": "Dr. Mehta", "starts_at": "2026-11-20T10:30"},
    )
    elder.post("/journal", {"kind": "question", "body": "Can I take it with tea?"})
    elder.post("/journal", {"kind": "symptom", "body": "Dizzy after walking"})

    page = elder.get("/appointments/1/summary").get_data(as_text=True)
    assert "Visit summary for Kamala" in page
    assert "Can I take it with tea?" in page
    assert "Dizzy after walking" in page
    assert "How medicines were taken" in page
    assert "Metformin" in page


def test_appointments_can_be_changed_and_cancelled(as_role):
    family = as_role(Role.FAMILY)
    family.post("/appointments/new", {"title": "Eye test", "starts_at": "2026-11-25T16:00"})
    family.post("/appointments/1/edit", {"title": "Eye check-up", "starts_at": "2026-11-26T16:00"})
    page = family.get("/appointments").get_data(as_text=True)
    assert "Eye check-up" in page and "Thursday 26 November" in page
    family.post("/appointments/1/cancel")
    assert "No appointments are booked" in family.get("/appointments").get_data(as_text=True)


def test_appointment_form_requires_a_date(as_role):
    response = as_role(Role.CARETAKER).post("/appointments/new", {"title": "Blood test"})
    assert response.status_code == 422
    assert "Choose the date and time" in response.get_data(as_text=True)


# --- managing the circle --------------------------------------------------


def test_inviting_someone_shows_their_passcode_once(app, as_role):
    elder = as_role(Role.ELDER)
    page = elder.post(
        "/circle/add", {"name": "Ravi", "role": "family", "receives_alerts": "1"}
    ).get_data(as_text=True)
    passcode = re.search(r"\d{4}-\d{4}", page)
    assert passcode is not None
    assert "shown only once" in page

    ravi = Browser(app.test_client())
    assert ravi.sign_in(passcode.group(0)).status_code == 302
    assert "Signed in as Ravi" in ravi.get("/today").get_data(as_text=True)


def test_passcodes_must_be_unique_within_a_circle(as_role):
    response = as_role(Role.ELDER).post(
        "/circle/add", {"name": "Ravi", "role": "family", "passcode": "family-pass-3"}
    )
    assert response.status_code == 422
    assert "already uses that passcode" in response.get_data(as_text=True)


def test_elder_can_change_their_own_passcode(app, as_role):
    elder = as_role(Role.ELDER)
    bad = elder.post(
        "/settings/passcode",
        {
            "current_passcode": "wrong",
            "new_passcode": "new-pass-99",
            "new_passcode_again": "new-pass-99",
        },
    )
    assert bad.status_code == 422
    elder.post(
        "/settings/passcode",
        {
            "current_passcode": "elder-pass-1",
            "new_passcode": "new-pass-99",
            "new_passcode_again": "new-pass-99",
        },
    )
    assert elder.get("/today").status_code == 200  # this browser stays signed in
    fresh = Browser(app.test_client())
    assert fresh.sign_in("elder-pass-1").status_code == 401
    assert fresh.sign_in("new-pass-99").status_code == 302


def test_settings_can_switch_off_the_quiet_checkin(app, as_role, circle):
    as_role(Role.ELDER).post(
        "/settings",
        {
            "elder_name": "Kamala",
            "email": "kamala@example.com",
            "timezone": "Asia/Kolkata",
            "checkin_hour": "11",
        },
    )
    with app.app_context():
        profile = profiles.get_profile(get_db(), circle.profile_id)
    assert profile is not None
    assert profile.checkin_enabled is False
    assert profile.checkin_hour == 11


# --- other pages ----------------------------------------------------------


def test_care_weather_page(app, as_role, circle):
    add_medicine(app, circle, medicine(stock_count=3), when=START - timedelta(days=8))
    page = as_role(Role.FAMILY).get("/insights").get_data(as_text=True)
    assert "This week:" in page
    assert "Time to refill" in page


def test_outbox_check_now_records_due_reminders(app, as_role, circle):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=2))
    carer = as_role(Role.CARETAKER)
    response = carer.post("/outbox/check", follow_redirects=True)
    page = response.get_data(as_text=True)
    assert "went out" in page
    assert "Demo mode" in page
    assert "Kamala has not confirmed the 8:00 AM dose" in page


def test_display_preferences_apply_to_every_page(browser: Browser, circle):
    browser.post("/preferences", {"text_size": "larger", "theme": "high", "next": "/"})
    assert 'class="size-larger theme-high"' in browser.get("/").get_data(as_text=True)


def test_unknown_pages_get_a_friendly_404(browser: Browser):
    response = browser.get("/no-such-page")
    assert response.status_code == 404
    assert "Page not found" in response.get_data(as_text=True)


def test_demo_mode_shows_example_sign_ins(tmp_path, clock):
    from hearth import create_app

    from .conftest import FAST_HASH

    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "demo.sqlite3"),
            "SECRET_KEY": "test",
            "SECRET_KEY_IS_TEMPORARY": False,
            "CLOCK": clock,
            "DEMO_MODE": True,
            "PASSCODE_HASH_METHOD": FAST_HASH,
        }
    )
    browser = Browser(app.test_client())
    page = browser.get("/").get_data(as_text=True)
    assert "DEMO-2026" in page and "sunrise-2468" in page
    assert browser.sign_in("sunrise-2468", code="DEMO-2026").status_code == 302
    assert "Kamala Rao" in browser.get("/today").get_data(as_text=True)
    assert "Care Weather for Kamala Rao" in browser.get("/outbox").get_data(as_text=True)
