"""Signing in and out, creating a circle, and display preferences."""

from __future__ import annotations

import sqlite3

from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from ..access import Role
from ..db import get_db, transaction
from ..repositories import profiles, records
from ..repositories.profiles import Member
from ..services import demo, passcodes, throttle
from ..timeutil import is_valid_zone, timezone_names
from . import forms, security
from .security import now

bp = Blueprint("auth", __name__)

DISPLAY_COOKIE = "hearth_display"
#: Display choices, each with the label shown in the settings. The first is the default.
TEXT_SIZES = (("normal", "Large"), ("large", "Larger"), ("larger", "Largest"))
# Light is the default on purpose: a setting someone picks is more predictable
# for older users than a device-wide dark mode they may not know is switched on.
THEMES = (("light", "Light"), ("dark", "Dark"), ("high", "High contrast"))

_SIGN_IN_FAILED = "That circle code and passcode do not match. Check both and try again."
_LOCKED = "Too many attempts. To keep the circle safe, sign-in is paused for 15 minutes."
_ONE_YEAR = 365 * 24 * 60 * 60


@bp.get("/")
def home() -> Response | str:
    if g.member is not None:
        return redirect(url_for("today.show"))
    return _home(error=None, code="")


def _home(*, error: str | None, code: str) -> str:
    """Render the sign-in page, with the example sign-ins when in demo mode."""
    demo_mode = current_app.config["DEMO_MODE"]
    return render_template(
        "home.html",
        error=error,
        circle_code=passcodes.format_circle_code(code) if code else "",
        demo_code=passcodes.format_circle_code(demo.DEMO_CIRCLE_CODE),
        demo_logins=demo.DEMO_PEOPLE if demo_mode else (),
    )


@bp.post("/sign-in")
def sign_in() -> Response | tuple[str, int]:
    db = get_db()
    moment = now()
    code = passcodes.normalise_circle_code(request.form.get("circle_code", ""))
    passcode = request.form.get("passcode", "")[: passcodes.MAX_PASSCODE_LENGTH]
    address = request.remote_addr or "unknown"
    buckets = {
        f"sign-in:{code}:{address}": throttle.LOGIN_PER_CIRCLE_AND_ADDRESS,
        f"sign-in:{code}": throttle.LOGIN_PER_CIRCLE,
        f"sign-in:address:{address}": throttle.LOGIN_PER_ADDRESS,
    }
    if any(throttle.locked_until(db, bucket, moment) for bucket in buckets):
        return _sign_in_error(_LOCKED, code, 429)

    member = _authenticate(db, code, passcode)
    if member is None:
        for bucket, rule in buckets.items():
            throttle.record_attempt(db, bucket, rule, moment)
        # One message for every failure, so it never reveals which part was wrong.
        return _sign_in_error(_SIGN_IN_FAILED, code, 401)

    throttle.clear(db, f"sign-in:{code}:{address}")
    security.sign_in(member)
    records.add_audit(
        db, profile_id=member.profile_id, member_id=member.id, action="Signed in", now=moment
    )
    if member.role is Role.ELDER:
        profiles.touch_elder_activity(db, member.profile_id, moment)
    return redirect(url_for("today.show"))


def _authenticate(db: sqlite3.Connection, code: str, passcode: str) -> Member | None:
    method = current_app.config["PASSCODE_HASH_METHOD"]
    profile = profiles.get_profile_by_code(db, code) if code else None
    if profile is None:
        passcodes.burn_verification_time(passcode, method)
        return None
    member_id = passcodes.find_member_id(db, profile.id, passcode)
    return profiles.get_member(db, profile.id, member_id) if member_id is not None else None


def _sign_in_error(message: str, code: str, status: int) -> tuple[str, int]:
    return _home(error=message, code=code), status


@bp.post("/sign-out")
def sign_out() -> Response:
    security.sign_out()
    return redirect(url_for("auth.home"))


@bp.route("/start", methods=["GET", "POST"])
def start() -> Response | str | tuple[str, int]:
    """Create a new care circle, owned by the elder."""
    if request.method == "GET":
        return _start_form({"timezone": "UTC"}, {})

    db = get_db()
    moment = now()
    form = request.form
    errors: forms.Errors = {}

    elder_name = forms.clean_line(form, "elder_name", 60)
    if not elder_name:
        errors["elder_name"] = "Enter the name of the person this circle is for."
    email = forms.clean_line(form, "email", 254)
    if email and not forms.is_email(email):
        errors["email"] = "That email address does not look right. Check it, or leave it empty."
    timezone = form.get("timezone", "")
    if not is_valid_zone(timezone):
        errors["timezone"] = "Choose a timezone from the list."
    passcode = form.get("passcode", "")
    problem = passcodes.passcode_problem(passcode)
    if problem:
        errors["passcode"] = problem
    elif passcode != form.get("passcode_again", ""):
        errors["passcode_again"] = "The two passcodes are not the same. Type them again."
    if errors:
        return _start_form(form, errors), 422

    bucket = f"new-circle:{request.remote_addr or 'unknown'}"
    if throttle.locked_until(db, bucket, moment):
        errors["form"] = "Too many circles were created from here. Please try again in an hour."
        return _start_form(form, errors), 429
    throttle.record_attempt(db, bucket, throttle.NEW_CIRCLES_PER_ADDRESS, moment)

    with transaction(db):
        code = _unused_circle_code(db)
        profile_id = profiles.create_profile(
            db, circle_code=code, elder_name=elder_name, timezone=timezone, now=moment
        )
        member_id = profiles.add_member(
            db,
            profile_id=profile_id,
            name=elder_name,
            email=email,
            role=Role.ELDER,
            passcode_hash=passcodes.hash_passcode(
                passcode, current_app.config["PASSCODE_HASH_METHOD"]
            ),
            now=moment,
        )
        records.add_audit(
            db, profile_id=profile_id, member_id=member_id, action="Created the circle", now=moment
        )
    member = profiles.get_member(db, profile_id, member_id)
    if member is not None:
        security.sign_in(member)
    return render_template(
        "start_done.html", new_circle_code=passcodes.format_circle_code(code), elder_name=elder_name
    )


def _unused_circle_code(db: sqlite3.Connection) -> str:
    while True:
        code = passcodes.generate_circle_code()
        if profiles.get_profile_by_code(db, code) is None:
            return code


def _start_form(values: forms.Form, errors: forms.Errors) -> str:
    return render_template("start.html", values=values, errors=errors, timezones=timezone_names())


@bp.post("/preferences")
def preferences() -> Response:
    """Remember the visitor's text size and colours in a cookie."""
    size = _choice(request.form.get("text_size", ""), TEXT_SIZES)
    theme = _choice(request.form.get("theme", ""), THEMES)
    response = redirect(_safe_next(request.form.get("next", "")))
    response.set_cookie(
        DISPLAY_COOKIE,
        f"{size}:{theme}",
        max_age=_ONE_YEAR,
        httponly=True,
        samesite="Lax",
        secure=current_app.config["SESSION_COOKIE_SECURE"],
    )
    return response


def read_display_preferences() -> dict[str, str]:
    """Parse the display cookie, ignoring anything unexpected."""
    size, _, theme = request.cookies.get(DISPLAY_COOKIE, "").partition(":")
    return {"text_size": _choice(size, TEXT_SIZES), "theme": _choice(theme, THEMES)}


def _choice(value: str, options: tuple[tuple[str, str], ...]) -> str:
    """Return ``value`` if it is one of the options, otherwise the default (first) one."""
    return value if value in {key for key, _ in options} else options[0][0]


def _safe_next(target: str) -> str:
    """Only ever redirect to a path on this site."""
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("auth.home")
