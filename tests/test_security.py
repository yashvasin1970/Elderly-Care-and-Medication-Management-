"""Sign-in, sessions, CSRF, permissions, isolation between circles and headers."""

from __future__ import annotations

import re
from datetime import timedelta

from hearth.access import Role
from hearth.db import get_db

from .conftest import CIRCLE_CODE, PASSCODES, START, Browser, add_medicine, medicine

GENERIC_FAILURE = "That circle code and passcode do not match"


# --- signing in -----------------------------------------------------------


def test_correct_passcode_signs_in(browser: Browser, circle):
    response = browser.sign_in(PASSCODES[Role.ELDER])
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/today")
    assert "Good morning, Kamala" in browser.get("/today").get_data(as_text=True)


def test_circle_code_is_forgiving_about_case_and_dashes(browser: Browser, circle):
    response = browser.sign_in(PASSCODES[Role.FAMILY], code="test-2345")
    assert response.status_code == 302


def test_wrong_passcode_and_unknown_circle_get_the_same_message(browser: Browser, circle):
    wrong_passcode = browser.sign_in("not-the-passcode")
    unknown_circle = browser.sign_in(PASSCODES[Role.ELDER], code="ZZZZ-ZZZZ")
    for response in (wrong_passcode, unknown_circle):
        assert response.status_code == 401
        assert GENERIC_FAILURE in response.get_data(as_text=True)


def test_repeated_guessing_locks_sign_in(browser: Browser, circle):
    for _ in range(5):
        assert browser.sign_in("guess-123456").status_code == 401
    locked = browser.sign_in(PASSCODES[Role.ELDER])
    assert locked.status_code == 429
    assert "paused for 15 minutes" in locked.get_data(as_text=True)


def test_lockout_expires(browser: Browser, circle, clock):
    for _ in range(5):
        browser.sign_in("guess-123456")
    clock.advance(minutes=16)
    assert browser.sign_in(PASSCODES[Role.ELDER]).status_code == 302


def test_passcodes_are_stored_only_as_hashes(app, circle):
    with app.app_context():
        stored = [row[0] for row in get_db().execute("SELECT passcode_hash FROM members")]
    assert stored
    for value in stored:
        assert value not in PASSCODES.values()
        assert value.startswith("pbkdf2:")


def test_signed_out_visitors_are_sent_to_the_start_page(browser: Browser, circle):
    response = browser.get("/medicines")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_sign_out_ends_the_session(as_role):
    elder = as_role(Role.ELDER)
    elder.post("/sign-out")
    assert elder.get("/today").status_code == 302


# --- sessions follow the elder's decisions immediately ---------------------


def test_removing_someone_ends_their_session_at_once(as_role, circle):
    elder, family = as_role(Role.ELDER), as_role(Role.FAMILY)
    assert family.get("/today").status_code == 200
    elder.post(f"/circle/{circle.members[Role.FAMILY]}/remove")
    assert family.get("/today").status_code == 302
    assert family.sign_in(PASSCODES[Role.FAMILY]).status_code == 401


def test_a_new_passcode_signs_the_old_session_out(as_role, circle):
    elder, carer = as_role(Role.ELDER), as_role(Role.CARETAKER)
    page = elder.post(f"/circle/{circle.members[Role.CARETAKER]}/passcode").get_data(as_text=True)
    new_passcode = re.search(r"\d{4}-\d{4}", page)
    assert new_passcode is not None
    assert carer.get("/today").status_code == 302
    fresh = Browser(carer.client.application.test_client())
    assert fresh.sign_in(PASSCODES[Role.CARETAKER]).status_code == 401
    assert fresh.sign_in(new_passcode.group(0)).status_code == 302


# --- CSRF -----------------------------------------------------------------


def test_forms_without_a_csrf_token_are_rejected(app, circle):
    client = app.test_client()
    response = client.post(
        "/sign-in", data={"circle_code": CIRCLE_CODE, "passcode": PASSCODES[Role.ELDER]}
    )
    assert response.status_code == 400


def test_forms_with_a_forged_csrf_token_are_rejected(as_role):
    elder = as_role(Role.ELDER)
    response = elder.client.post(
        "/journal", data={"csrf_token": "forged", "kind": "note", "body": "x"}
    )
    assert response.status_code == 400


# --- permissions ----------------------------------------------------------


def test_viewers_can_look_but_not_change(as_role):
    viewer = as_role(Role.VIEWER)
    assert viewer.get("/medicines").status_code == 200
    assert viewer.get("/medicines/new").status_code == 403
    assert viewer.get("/appointments/new").status_code == 403
    assert viewer.get("/outbox").status_code == 403
    assert viewer.post("/medicines/draft", {"sentence": "Aspirin 75 mg"}).status_code == 403


def test_only_the_elder_manages_the_circle_and_settings(as_role, circle):
    for role in (Role.CARETAKER, Role.FAMILY):
        person = as_role(role)
        assert person.get("/circle/add").status_code == 403
        assert person.get("/settings").status_code == 403
        assert person.post(f"/circle/{circle.members[Role.VIEWER]}/remove").status_code == 403


def test_family_cannot_record_doses(app, as_role, circle):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=2))
    elder_page = as_role(Role.ELDER).get("/today").get_data(as_text=True)
    action = re.search(r"/doses/\d+/2026-11-16/take", elder_page)
    assert action is not None
    assert as_role(Role.FAMILY).post(action.group(0)).status_code == 403


def test_the_elder_cannot_be_removed_by_anyone(as_role, circle):
    elder = as_role(Role.ELDER)
    assert elder.post(f"/circle/{circle.members[Role.ELDER]}/remove").status_code == 404


# --- one circle can never reach another -----------------------------------


def _second_circle(app) -> Browser:
    other = Browser(app.test_client())
    response = other.post(
        "/start",
        {
            "elder_name": "Someone Else",
            "timezone": "Europe/London",
            "passcode": "other-circle-1",
            "passcode_again": "other-circle-1",
        },
    )
    assert response.status_code == 200
    return other


def test_another_circle_cannot_see_or_change_this_circles_medicines(app, circle):
    add_medicine(app, circle, medicine("Secret Med"), when=START - timedelta(days=2))
    outsider = _second_circle(app)
    assert "Secret Med" not in outsider.get("/medicines").get_data(as_text=True)
    assert outsider.get("/medicines/1/edit").status_code == 404
    assert outsider.post("/medicines/1/stop").status_code == 404
    assert outsider.get("/requests").status_code == 200


def test_another_circle_cannot_record_this_circles_doses(app, circle, as_role):
    add_medicine(app, circle, medicine(), when=START - timedelta(days=2))
    outsider = _second_circle(app)
    response = outsider.post("/doses/1/2026-11-16/take", follow_redirects=True)
    assert "That dose could not be found" in response.get_data(as_text=True)
    page = as_role(Role.ELDER).get("/today").get_data(as_text=True)
    assert "Taken at" not in page


# --- output encoding and redirects ---------------------------------------


def test_user_input_is_escaped_in_pages(as_role):
    elder = as_role(Role.ELDER)
    elder.post("/journal", {"kind": "note", "body": "<script>alert('x')</script>"})
    page = elder.get("/journal").get_data(as_text=True)
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert" in page


def test_preferences_only_redirect_within_the_site(browser: Browser, circle):
    for target in ("https://evil.example/", "//evil.example", "/\\evil.example"):
        response = browser.post("/preferences", {"text_size": "large", "next": target})
        assert response.headers["Location"] == "/"
    ok = browser.post("/preferences", {"text_size": "large", "next": "/medicines"})
    assert ok.headers["Location"] == "/medicines"


# --- headers and cookies --------------------------------------------------


def test_security_headers_are_set(browser: Browser, circle):
    headers = browser.get("/").headers
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "same-origin"
    assert headers["Cache-Control"] == "no-store"


def test_session_cookie_is_http_only_and_same_site(browser: Browser, circle):
    response = browser.sign_in(PASSCODES[Role.ELDER])
    cookie = next(v for v in response.headers.getlist("Set-Cookie") if "hearth_session" in v)
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


# --- the scheduler endpoint ----------------------------------------------


def test_task_endpoint_requires_the_shared_token(app, circle):
    client = app.test_client()
    assert client.post("/tasks/reminders").status_code == 403
    wrong = {"Authorization": "Bearer nope"}
    assert client.post("/tasks/reminders", headers=wrong).status_code == 403
    right = {"Authorization": "Bearer task-token-for-tests"}
    response = client.post("/tasks/reminders", headers=right)
    assert response.status_code == 200
    assert response.get_json() == {"sent": 0}


def test_task_endpoint_does_not_exist_without_a_configured_token(app, circle):
    app.config["TASK_TOKEN"] = None
    assert app.test_client().post("/tasks/reminders").status_code == 404


def test_health_check(app):
    assert app.test_client().get("/healthz").get_json() == {"status": "ok"}
