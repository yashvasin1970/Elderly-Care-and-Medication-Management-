"""Each kind of member gets their own view, plus the help button and demo tools."""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from hearth.access import Role
from hearth.db import get_db
from hearth.engine.insights import DoseRecord, band_for
from hearth.engine.models import DoseStatus
from hearth.repositories import profiles, records
from hearth.web.today import week_bars

from .conftest import START, Browser, add_medicine, medicine
from .factories import local

# --- one person, one view -------------------------------------------------


def test_the_elder_gets_the_senior_view(app, as_role, circle):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=2))
    page = as_role(Role.ELDER).get("/today").get_data(as_text=True)
    assert "layout-senior" in page
    assert "Time to take now" in page
    assert "I need help" in page
    assert "Caretaker dashboard" not in page


def test_the_caretaker_gets_the_dashboard(app, as_role, circle):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=2))
    page = as_role(Role.CARETAKER).get("/today").get_data(as_text=True)
    assert "layout-dashboard" in page
    assert "<h1>Caretaker dashboard</h1>" in page
    assert "Taken this week" in page and "Missed or late today" in page
    assert 'aria-label="Doses taken on each of the last 7 days"' in page
    assert "Mark taken" in page
    assert "I need help" not in page


def test_family_get_an_overview_without_dose_buttons(app, as_role, circle):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=2))
    page = as_role(Role.FAMILY).get("/today").get_data(as_text=True)
    assert "<h1>Family overview</h1>" in page
    assert "How Kamala is doing" in page
    assert not re.search(r"/doses/\d+/[\d-]+/take", page)
    assert "Doses are recorded by Kamala or a caretaker" in page


def test_the_dashboard_counts_todays_doses(app, as_role, circle):
    add_medicine(app, circle, medicine("Metformin"), when=START - timedelta(days=2))
    carer = as_role(Role.CARETAKER)
    page = carer.get("/today").get_data(as_text=True)
    assert "0 missed, 1 waiting now" in page  # the 8:00 dose is late at 9:00
    carer.post(re.search(r"/doses/\d+/[\d-]+/take", page).group(0))
    page = carer.get("/today").get_data(as_text=True)
    assert "All on track" in page
    assert "Taken at 9:00 AM by Joseph" in page


# --- the 7-day chart ------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "band"), [(None, "none"), (100, "good"), (90, "good"), (89, "fair"), (74, "low")]
)
def test_chart_bands(rate, band):
    assert band_for(rate) == band


def test_week_bars_cover_every_day_including_empty_ones():
    today = date(2026, 11, 16)
    records_ = [
        DoseRecord(1, "Metformin", local(2026, 11, 15, 8), DoseStatus.TAKEN),
        DoseRecord(1, "Metformin", local(2026, 11, 15, 20), DoseStatus.MISSED),
        DoseRecord(1, "Metformin", local(2026, 11, 16, 8), DoseStatus.TAKEN),
    ]
    bars = week_bars(records_, today)
    assert [bar.day for bar in bars] == [today - timedelta(days=n) for n in range(6, -1, -1)]
    assert bars[0].band == "none" and bars[0].height == 0
    assert (bars[-2].adherence.rate, bars[-2].band, bars[-2].height) == (50, "low", 50)
    assert (bars[-1].band, bars[-1].height) == ("good", 100)


# --- "I need help" --------------------------------------------------------


def test_help_button_tells_everyone_who_supports_the_elder(app, as_role, circle):
    elder = as_role(Role.ELDER)
    page = elder.post("/help", follow_redirects=True).get_data(as_text=True)
    assert "Joseph and Priya have been asked to get in touch with you" in page
    with app.app_context():
        notes = records.list_notifications(get_db(), circle.profile_id, kinds=("help",))
    assert sorted(n.recipient for n in notes) == ["joseph@example.com", "priya@example.com"]
    assert notes[0].subject == "Kamala asked for help"

    carer_page = as_role(Role.CARETAKER).get("/today").get_data(as_text=True)
    assert "Kamala asked for help at 9:00 AM" in carer_page


def test_pressing_help_twice_in_a_minute_sends_once(app, as_role, circle):
    elder = as_role(Role.ELDER)
    elder.post("/help")
    elder.post("/help")
    with app.app_context():
        notes = records.list_notifications(get_db(), circle.profile_id, kinds=("help",))
    assert len(notes) == 2  # one each for Joseph and Priya


def test_the_help_banner_fades_after_a_few_hours(app, as_role, circle, clock):
    as_role(Role.ELDER).post("/help")
    clock.advance(hours=4)
    page = as_role(Role.CARETAKER).get("/today").get_data(as_text=True)
    assert 'class="help-banner"' not in page
    assert "Kamala asked for help" in page  # still listed under recent alerts


def test_only_the_elder_has_a_help_button(as_role):
    for role in (Role.CARETAKER, Role.FAMILY, Role.VIEWER):
        assert as_role(role).post("/help").status_code == 403


def test_help_with_no_one_to_tell_says_so(app, as_role, circle):
    with app.app_context():
        db = get_db()
        for role in (Role.CARETAKER, Role.FAMILY):
            profiles.deactivate_member(db, circle.profile_id, circle.members[role])
    page = as_role(Role.ELDER).post("/help", follow_redirects=True).get_data(as_text=True)
    assert "no one was told" in page
    assert "call your local emergency number" in page


# --- demo tools -----------------------------------------------------------


def test_test_alerts_do_not_exist_outside_demo_mode(app, as_role):
    carer = as_role(Role.CARETAKER)
    assert carer.post("/demo/test-alert", {"kind": "missed"}).status_code == 404
    assert "See the alerts working" not in carer.get("/today").get_data(as_text=True)


def test_test_alerts_send_marked_emails_in_demo_mode(app, as_role, circle):
    app.config["DEMO_MODE"] = True
    add_medicine(app, circle, medicine("Insulin", "critical"), when=START - timedelta(days=2))
    carer = as_role(Role.CARETAKER)
    assert "See the alerts working" in carer.get("/today").get_data(as_text=True)

    page = carer.post("/demo/test-alert", {"kind": "missed"}, follow_redirects=True)
    text = page.get_data(as_text=True)
    assert "Test sent: 1 email" in text
    assert "[Test] Kamala has not confirmed the 8:00 AM dose" in text

    carer.post("/demo/test-alert", {"kind": "upcoming"})
    with app.app_context():
        notes = records.list_notifications(get_db(), circle.profile_id, kinds=("test",))
    assert {n.recipient for n in notes} == {"joseph@example.com", "kamala@example.com"}
    assert all(n.subject.startswith("[Test]") for n in notes)


def test_unknown_test_alert_is_rejected(app, as_role):
    app.config["DEMO_MODE"] = True
    assert as_role(Role.CARETAKER).post("/demo/test-alert", {"kind": "boom"}).status_code == 400


def test_family_and_viewers_cannot_use_demo_tools_they_lack_access_to(app, as_role):
    app.config["DEMO_MODE"] = True
    assert as_role(Role.VIEWER).post("/demo/test-alert", {"kind": "missed"}).status_code == 403


# --- one-click demo sign-in -----------------------------------------------


def test_demo_sign_in_offers_each_view(tmp_path, clock):
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
    for view in ("Senior view", "Caretaker dashboard", "Family overview", "Read-only view"):
        assert view in page
    assert browser.sign_in("caring-1357", code="DEMO-2026").status_code == 302
    assert "<h1>Caretaker dashboard</h1>" in browser.get("/today").get_data(as_text=True)
