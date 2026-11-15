"""Today: the first screen after signing in, different for each kind of member.

* The elder gets the Senior view. It answers one question first, "is there
  anything to take right now?", then shows the rest of the day grouped into
  morning, afternoon, evening and night, with a large "I need help" button.
* Caretakers, family and viewers get a dashboard: headline numbers, a 7-day
  chart, today's medicines, the next doctor visit and recent alerts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from ..access import Permission, Role
from ..db import get_db
from ..engine.insights import Adherence, DoseRecord, band_for, summarise
from ..engine.models import DoseStatus
from ..engine.schedule import PARTS_OF_DAY, part_of_day
from ..repositories import appointments, medications, profiles, records
from ..services import alerts, care, consent
from ..services.care import CareContext, DoseView
from ..services.mailer import mailer_from_config
from ..timeutil import friendly_clock, parse_date
from .security import allowed, current_member, current_profile, now, require

bp = Blueprint("today", __name__)

#: Doses that are waiting for someone to act on them right now.
_OPEN = (DoseStatus.DUE, DoseStatus.LATE, DoseStatus.SNOOZED)
_DONE_MESSAGES = {
    "take": "Well done. {name} is recorded as taken.",
    "skip": "{name} is recorded as skipped. That is your decision to make.",
    "snooze": f"All right. {{name}} will come up again in {care.SNOOZE_MINUTES} minutes.",
    "undo": "The record for {name} has been cleared.",
}
_DASHBOARD_TITLES = {
    Role.CARETAKER: "Caretaker dashboard",
    Role.FAMILY: "Family overview",
    Role.VIEWER: "Overview",
}
#: Alerts worth showing on a dashboard (reminders to the elder are left out).
_ALERT_KINDS = ("help", "missed", "checkin", "refill", "appointment", "test")
_CHART_DAYS = 7


@dataclass(frozen=True, slots=True)
class DayPart:
    """The doses that fall in one part of the day."""

    key: str
    label: str
    doses: list[DoseView]


@dataclass(frozen=True, slots=True)
class DayBar:
    """One bar of the 7-day chart."""

    day: date
    adherence: Adherence
    band: str
    height: int  # percentage of the chart height, in steps of 5


@bp.get("/today")
@require(Permission.VIEW_CARE)
def show() -> str:
    db = get_db()
    member = current_member()
    ctx = care.load_context(db, current_profile())
    moment = now()
    if member.role is Role.ELDER:
        return _senior_view(db, ctx, moment)
    return _dashboard(db, ctx, member.role, moment)


def _senior_view(db: sqlite3.Connection, ctx: CareContext, moment: datetime) -> str:
    profile = ctx.profile
    local_now = ctx.local(moment)
    today = local_now.date()
    doses_today = care.doses_for_day(db, ctx, today, moment)
    take_now = _carried_over(db, ctx, today, moment) + [v for v in doses_today if v.status in _OPEN]
    next_dose = next((v for v in doses_today if v.status is DoseStatus.UPCOMING), None)
    taken = sum(1 for view in doses_today if view.status is DoseStatus.TAKEN)
    pending = medications.list_requests(db, profile.id, status="pending")

    return render_template(
        "today.html",
        greeting=_greeting(local_now),
        local_now=local_now,
        total=len(doses_today),
        taken=taken,
        take_now=take_now,
        next_dose=next_dose,
        day_parts=group_by_part_of_day([v for v in doses_today if v.status not in _OPEN]),
        visits=appointments.list_between(db, profile.id, moment, moment + timedelta(days=7)),
        refills=[f for f in care.refill_forecasts(ctx, today) if f.needs_refill],
        suggestions=care.rhythm_suggestions(db, ctx, moment),
        pending=[(item, consent.describe_request(db, profile.id, item)) for item in pending],
        names=profiles.member_names(db, profile.id),
        spoken=spoken_summary(
            whose="You have",
            taken=taken,
            total=len(doses_today),
            take_now=take_now,
            next_dose=next_dose,
        ),
    )


def _dashboard(db: sqlite3.Connection, ctx: CareContext, role: Role, moment: datetime) -> str:
    profile = ctx.profile
    local_now = ctx.local(moment)
    today = local_now.date()
    doses_today = care.doses_for_day(db, ctx, today, moment)
    statuses = [view.status for view in doses_today]
    upcoming = [v for v in doses_today if v.status is DoseStatus.UPCOMING]
    week = care.outcomes_between(db, ctx, today - timedelta(days=_CHART_DAYS - 1), today, moment)
    next_visits = appointments.list_upcoming(db, profile.id, moment, limit=1)
    next_visit = next_visits[0] if next_visits else None

    return render_template(
        "dashboard.html",
        title=_DASHBOARD_TITLES.get(role, "Overview"),
        is_family=role is Role.FAMILY,
        local_now=local_now,
        rows=_carried_over(db, ctx, today, moment) + doses_today,
        total=len(doses_today),
        taken=statuses.count(DoseStatus.TAKEN),
        missed=statuses.count(DoseStatus.MISSED),
        late=sum(1 for status in statuses if status in _OPEN),
        upcoming=upcoming,
        week=summarise(week),
        bars=week_bars(week, today),
        report=care.care_weather_report(db, ctx, moment),
        suggestions=care.rhythm_suggestions(db, ctx, moment),
        next_visit=next_visit,
        days_to_visit=(ctx.local(next_visit.starts_at).date() - today).days if next_visit else None,
        refills=[f for f in care.refill_forecasts(ctx, today) if f.needs_refill],
        waiting=medications.count_pending_requests(db, profile.id),
        alerts=(
            records.list_notifications(db, profile.id, limit=5, kinds=_ALERT_KINDS)
            if allowed(Permission.VIEW_ACTIVITY)
            else []
        ),
        help_at=alerts.recent_help_request(db, profile.id, moment),
        names=profiles.member_names(db, profile.id),
        # Test buttons sit on the caretaker's dashboard, where alerts are acted on.
        demo_tools=current_app.config["DEMO_MODE"] and role is Role.CARETAKER,
    )


def _carried_over(
    db: sqlite3.Connection, ctx: CareContext, today: date, moment: datetime
) -> list[DoseView]:
    """Last night's doses that are still open: they can be confirmed after midnight."""
    yesterday = today - timedelta(days=1)
    return [v for v in care.doses_for_day(db, ctx, yesterday, moment) if v.status in _OPEN]


# --- actions --------------------------------------------------------------


@bp.post("/doses/<int:slot_id>/<dose_date>/<action>")
@require(Permission.LOG_DOSES)
def record(slot_id: int, dose_date: str, action: str) -> Response:
    try:
        day = parse_date(dose_date)
    except ValueError:
        abort(404)
    db = get_db()
    ctx = care.load_context(db, current_profile())
    try:
        occurrence = care.record_dose(
            db, ctx, current_member(), slot_id=slot_id, dose_date=day, action=action, now=now()
        )
    except care.DoseError as error:
        flash(str(error), "error")
    else:
        flash(_DONE_MESSAGES[action].format(name=occurrence.medication.name), "success")
    return redirect(url_for("today.show"))


@bp.post("/help")
@require(Permission.ASK_FOR_HELP)
def ask_for_help() -> Response:
    """The elder's "I need help" button: tell everyone who supports them, now."""
    config = current_app.config
    told = alerts.ask_for_help(
        get_db(),
        mailer_from_config(config),
        current_profile(),
        current_member(),
        now(),
        config["PUBLIC_BASE_URL"],
    )
    emergency = "If it is an emergency, call your local emergency number."
    if told:
        who = told[0] if len(told) == 1 else ", ".join(told[:-1]) + " and " + told[-1]
        verb = "has" if len(told) == 1 else "have"
        flash(f"Done. {who} {verb} been asked to get in touch with you. {emergency}", "success")
    else:
        flash(
            f"No one in your circle has an email address yet, so no one was told. {emergency}",
            "error",
        )
    return redirect(url_for("today.show"))


@bp.post("/suggestions")
@require(Permission.MANAGE_ROUTINE)
def decide_suggestion() -> Response:
    """Accept or dismiss a suggested change to a reminder time."""
    db = get_db()
    profile = current_profile()
    moment = now()
    key = request.form.get("key", "")[:80]
    if not key.startswith("rhythm:"):
        abort(400)
    if request.form.get("decision") == "apply":
        ctx = care.load_context(db, profile)
        if care.apply_rhythm_suggestion(db, ctx, key, current_member(), moment):
            flash("Done. Your reminders now match your routine.", "success")
        else:
            flash("That suggestion no longer applies, so nothing was changed.", "info")
    else:
        profiles.dismiss_suggestion(db, profile.id, key, moment)
        flash("No problem. Your reminder times stay as they are.", "info")
    return redirect(url_for("today.show"))


# --- helpers --------------------------------------------------------------


def group_by_part_of_day(views: Sequence[DoseView]) -> list[DayPart]:
    """Group doses into morning, afternoon, evening and night, leaving out empty parts."""
    groups: dict[str, list[DoseView]] = {name: [] for name, _ in PARTS_OF_DAY}
    for view in views:
        groups[part_of_day(view.occurrence.scheduled_at)].append(view)
    return [
        DayPart(name, name.capitalize(), groups[name]) for name, _ in PARTS_OF_DAY if groups[name]
    ]


def week_bars(records_: Sequence[DoseRecord], today: date, days: int = _CHART_DAYS) -> list[DayBar]:
    """One bar per day for the last ``days`` days, including days with no doses."""
    bars = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        adherence = summarise([r for r in records_ if r.scheduled_at.date() == day])
        rate = adherence.rate
        height = 0 if rate is None else round(rate / 5) * 5
        bars.append(DayBar(day, adherence, band_for(rate), height))
    return bars


def spoken_summary(
    *,
    whose: str,
    taken: int,
    total: int,
    take_now: Sequence[DoseView],
    next_dose: DoseView | None,
) -> str:
    """A short, plain summary for the "Read this to me" button."""
    if total == 0 and not take_now:
        return "There are no medicines to take today."
    parts = [f"{whose} taken {taken} of {total} medicines today."]
    if take_now:
        names = ", ".join(_spoken_name(view) for view in take_now)
        parts.append(f"It is time to take: {names}.")
    elif next_dose is not None:
        when = friendly_clock(next_dose.occurrence.scheduled_at)
        parts.append(
            f"Next is {_spoken_name(next_dose)}, {next_dose.when_label.lower()}, at {when}."
        )
    else:
        parts.append("Nothing more is due today.")
    return " ".join(parts)


def _spoken_name(view: DoseView) -> str:
    medication = view.occurrence.medication
    return f"{medication.name} {medication.dose}".strip()


def _greeting(local_now: datetime) -> str:
    if local_now.hour < 12:
        return "Good morning"
    return "Good afternoon" if local_now.hour < 17 else "Good evening"
