"""Defence in depth: passcode strength, session lifetimes, attempt visibility, headers."""

from __future__ import annotations

import pytest

from hearth import create_app
from hearth.access import Role
from hearth.db import get_db
from hearth.repositories import records
from hearth.services import passcodes

from .conftest import CIRCLE_CODE, FAST_HASH, PASSCODES, Browser

# --- passcode strength ----------------------------------------------------


@pytest.mark.parametrize(
    "weak",
    [
        "1234567",  # too short
        "12345678",  # the most common passcode of all
        "Password1",  # common, whatever the capitals
        "pass-word",  # common once punctuation is ignored
        "11111111",  # one character repeated
        "abababab",  # two characters
        "abcdefgh",  # a run of letters
        "98765432",  # a run of digits, backwards
        "123123123",  # a short repeating unit
    ],
)
def test_weak_passcodes_are_refused(weak):
    assert passcodes.passcode_problem(weak) is not None


@pytest.mark.parametrize("strong", ["river-stone-7", "sunrise-2468", "Blue 4 teapots", "8261-3947"])
def test_reasonable_passcodes_are_accepted(strong):
    assert passcodes.passcode_problem(strong) is None


def test_a_passcode_containing_the_persons_name_is_refused():
    problem = passcodes.passcode_problem("kamala-2024", avoid=("Kamala Rao",))
    assert problem is not None and "name" in problem
    # Very short name parts are not treated as names to avoid.
    assert passcodes.passcode_problem("dr-river-77", avoid=("Dr. Li",)) is None


def test_generated_passcodes_always_pass_the_rules():
    for _ in range(200):
        assert passcodes.passcode_problem(passcodes.generate_passcode()) is None


def test_creating_a_circle_refuses_a_passcode_with_the_elders_name(browser: Browser):
    response = browser.post(
        "/start",
        {
            "elder_name": "Asha Menon",
            "timezone": "Asia/Kolkata",
            "passcode": "asha-1958!",
            "passcode_again": "asha-1958!",
        },
    )
    assert response.status_code == 422
    assert "Don&#39;t use a name in the passcode" in response.get_data(as_text=True)


# --- session lifetimes ----------------------------------------------------


def test_an_idle_session_ends_after_twelve_hours(as_role, clock):
    elder = as_role(Role.ELDER)
    clock.advance(hours=11)
    assert elder.get("/today").status_code == 200  # still active, and the idle clock resets
    clock.advance(hours=11)
    assert elder.get("/today").status_code == 200
    clock.advance(hours=13)
    response = elder.get("/today")
    assert response.status_code == 302
    assert "You were signed out to keep your circle safe" in elder.get("/").get_data(as_text=True)


def test_a_busy_session_still_ends_after_three_days(as_role, clock):
    elder = as_role(Role.ELDER)
    for _ in range(6):  # used every 11 hours: never idle, 66 hours in total
        clock.advance(hours=11)
        assert elder.get("/today").status_code == 200
    clock.advance(hours=7)  # 73 hours after signing in
    assert elder.get("/today").status_code == 302


def test_sign_out_everywhere_ends_every_session_of_that_person_only(app, as_role):
    phone, laptop = as_role(Role.FAMILY), as_role(Role.FAMILY)
    elder = as_role(Role.ELDER)
    page = phone.post("/sign-out-everywhere", follow_redirects=True).get_data(as_text=True)
    assert "signed out on every device" in page
    assert laptop.get("/today").status_code == 302
    assert elder.get("/today").status_code == 200  # other people are not affected
    assert "Signed out on all devices" in elder.get("/history").get_data(as_text=True)


def test_sign_out_everywhere_needs_a_csrf_token(app, as_role):
    family = as_role(Role.FAMILY)
    assert family.client.post("/sign-out-everywhere").status_code == 400


# --- wrong passcodes are visible ------------------------------------------


def test_wrong_passcodes_appear_in_the_circles_history(app, circle, as_role):
    Browser(app.test_client()).sign_in("not-the-passcode")
    history = as_role(Role.ELDER).get("/history").get_data(as_text=True)
    assert "Someone tried a wrong passcode" in history


def test_a_lockout_warns_the_elder_by_email_once(app, circle, as_role):
    guesser = Browser(app.test_client())
    for _ in range(6):
        guesser.sign_in("guess-what-77")
    with app.app_context():
        notes = records.list_notifications(get_db(), circle.profile_id, kinds=("security",))
        history = records.list_audit(get_db(), circle.profile_id)
    assert [n.recipient for n in notes] == ["kamala@example.com"]
    assert "Several wrong passcodes were tried" in notes[0].subject
    assert "guess-what-77" not in notes[0].body  # never repeat what was tried
    assert any(entry.action.startswith("Sign-in paused") for entry in history)


def test_wrong_passcodes_for_an_unknown_circle_record_nothing(app, circle):
    Browser(app.test_client()).sign_in(PASSCODES[Role.ELDER], code="ZZZZ-ZZZZ")
    with app.app_context():
        actions = [e.action for e in records.list_audit(get_db(), circle.profile_id)]
    assert "Someone tried a wrong passcode" not in actions


# --- headers and cookies over HTTPS ---------------------------------------


def test_extra_isolation_headers_are_always_sent(browser: Browser):
    headers = browser.get("/").headers
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert headers["X-Permitted-Cross-Domain-Policies"] == "none"
    assert "Strict-Transport-Security" not in headers  # only over HTTPS


def test_https_deployments_get_hsts_and_a_host_only_cookie(tmp_path, clock, monkeypatch):
    monkeypatch.setenv("HEARTH_SECURE_COOKIES", "1")
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "https.sqlite3"),
            "SECRET_KEY": "test",
            "SECRET_KEY_IS_TEMPORARY": False,
            "CLOCK": clock,
            "PASSCODE_HASH_METHOD": FAST_HASH,
        }
    )
    assert app.config["SESSION_COOKIE_NAME"] == "__Host-hearth_session"
    client = app.test_client()
    response = client.get("/", base_url="https://localhost")
    assert "max-age=31536000" in response.headers["Strict-Transport-Security"]
    assert "upgrade-insecure-requests" in response.headers["Content-Security-Policy"]


def test_the_circle_code_is_kept_tidy_after_a_failed_sign_in(browser: Browser, circle):
    page = browser.sign_in("wrong-passcode-1", code="test 2345").get_data(as_text=True)
    assert f'value="{CIRCLE_CODE[:4]}-{CIRCLE_CODE[4:]}"' in page
