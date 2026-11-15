"""The web layer: pages, form handling and browser security."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from flask import Flask, Response, current_app, g, redirect, render_template, url_for
from werkzeug.exceptions import HTTPException

from ..access import ROLE_LABELS, Permission, Role
from ..db import get_db
from ..engine.models import DoseStatus
from ..repositories import medications as medication_store
from ..repositories.profiles import Member
from ..services.passcodes import format_circle_code
from ..timeutil import friendly_clock, get_zone
from . import appointments, auth, circle, insights, medications, security, tasks, today

_BLUEPRINTS = (auth.bp, today.bp, medications.bp, appointments.bp, circle.bp, insights.bp, tasks.bp)

STATUS_LABELS = {
    DoseStatus.TAKEN: "Taken",
    DoseStatus.SKIPPED: "Skipped",
    DoseStatus.SNOOZED: "Reminder set",
    DoseStatus.UPCOMING: "Coming up",
    DoseStatus.DUE: "Due now",
    DoseStatus.LATE: "Late",
    DoseStatus.MISSED: "Missed",
}

_ERROR_PAGES = {
    400: ("That did not work", "The page was out of date. Please go back and try again."),
    403: ("Not available to you", "Your role in this circle does not include this."),
    404: ("Page not found", "That page does not exist, or is no longer available."),
    413: ("Too much to send", "That was more than a form can accept. Please shorten it."),
    429: ("Please wait a little", "Too many attempts. Try again in a few minutes."),
}
_DEFAULT_ERROR = ("Something went wrong", "It is not your fault. Please try again in a moment.")


def register(app: Flask) -> None:
    """Attach pages, template helpers, security and error pages to the app."""
    security.init_app(app)
    for blueprint in _BLUEPRINTS:
        app.register_blueprint(blueprint)
    app.context_processor(_template_context)
    # Globals are also visible inside imported macros, unlike context values.
    app.jinja_env.globals.update(csrf_token=security.csrf_token, status_labels=STATUS_LABELS)
    app.register_error_handler(HTTPException, _error_page)
    for name, function in _FILTERS.items():
        app.add_template_filter(function, name)


# --- template helpers -----------------------------------------------------


def _local(value: date | datetime) -> date | datetime:
    """Convert a moment to the elder's timezone; plain dates pass through."""
    if isinstance(value, datetime):
        profile = g.get("profile")
        return value.astimezone(get_zone(profile.timezone if profile else "UTC"))
    return value


def _clock(value: datetime) -> str:
    return friendly_clock(_local(value))  # type: ignore[arg-type]


def _long_date(value: date | datetime) -> str:
    value = _local(value)
    return f"{value:%A} {value.day} {value:%B}"


def _short_date(value: date | datetime) -> str:
    value = _local(value)
    return f"{value:%a} {value.day} {value:%b}"


def _day_and_time(value: datetime) -> str:
    return f"{_short_date(value)}, {_clock(value)}"


def _machine(value: date | datetime) -> str:
    """ISO form for the ``datetime`` attribute of a ``<time>`` element."""
    value = _local(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    return value.isoformat()


_FILTERS = {
    "local": _local,
    "clock": _clock,
    "long_date": _long_date,
    "short_date": _short_date,
    "day_and_time": _day_and_time,
    "machine": _machine,
}


def _template_context() -> dict[str, Any]:
    member = g.get("member")
    profile = g.get("profile")
    pending_count = 0
    if member is not None:
        pending_count = medication_store.count_pending_requests(get_db(), profile.id)
    return {
        "member": member,
        "profile": profile,
        # The elder gets the large, simple Senior layout; everyone else a dashboard.
        "layout": _layout(member),
        "moment": security.now() if member is not None else None,
        "demo_mode": current_app.config["DEMO_MODE"],
        "may": lambda name: security.allowed(Permission(name)),
        "display": auth.read_display_preferences(),
        "text_sizes": auth.TEXT_SIZES,
        "themes": auth.THEMES,
        "pending_count": pending_count,
        "circle_code": format_circle_code(profile.circle_code) if profile else "",
        "role_label": ROLE_LABELS[member.role] if member else "",
    }


def _layout(member: Member | None) -> str:
    if member is None:
        return "public"
    return "senior" if member.role is Role.ELDER else "dashboard"


# --- error pages ----------------------------------------------------------


def _error_page(error: HTTPException) -> Response | tuple[str, int]:
    code = error.code or 500
    if code == 401:
        return redirect(url_for("auth.home"))
    title, message = _ERROR_PAGES.get(code, _DEFAULT_ERROR)
    if code == 400 and error.description:
        message = str(error.description)
    return render_template("error.html", title=title, message=message, code=code), code
