"""The Today screen: the elder's day at a glance, and recording doses."""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ..access import Permission
from ..db import get_db
from ..engine.models import DoseStatus
from ..repositories import appointments, medications, profiles
from ..services import care, consent
from ..services.care import DoseView
from ..timeutil import friendly_clock, parse_date
from .security import allowed, current_member, current_profile, now, require

bp = Blueprint("today", __name__)

_OPEN = (DoseStatus.DUE, DoseStatus.LATE, DoseStatus.SNOOZED)
_AHEAD = (*_OPEN, DoseStatus.UPCOMING)
_DONE_MESSAGES = {
    "take": "{name} is recorded as taken.",
    "skip": "{name} is recorded as skipped. That is your decision to make.",
    "snooze": f"All right. {{name}} will come up again in {care.SNOOZE_MINUTES} minutes.",
    "undo": "The record for {name} has been cleared.",
}


@bp.get("/today")
@require(Permission.VIEW_CARE)
def show() -> str:
    db = get_db()
    profile = current_profile()
    member = current_member()
    moment = now()
    ctx = care.load_context(db, profile)
    local_now = ctx.local(moment)
    today = local_now.date()

    doses_today = care.doses_for_day(db, ctx, today, moment)
    # A late-evening dose can still be confirmed after midnight.
    carried_over = [
        view
        for view in care.doses_for_day(db, ctx, today - timedelta(days=1), moment)
        if view.status in _OPEN
    ]
    taken = sum(1 for view in doses_today if view.status is DoseStatus.TAKEN)
    next_dose = next((v for v in carried_over + doses_today if v.status in _AHEAD), None)

    is_owner = allowed(Permission.DECIDE_CHANGES)
    pending = medications.list_requests(db, profile.id, status="pending") if is_owner else []
    names = profiles.member_names(db, profile.id)

    return render_template(
        "today.html",
        greeting=_greeting(local_now),
        local_now=local_now,
        doses_today=doses_today,
        carried_over=carried_over,
        taken=taken,
        next_dose=next_dose,
        visits=appointments.list_between(db, profile.id, moment, moment + timedelta(days=7)),
        refills=[f for f in care.refill_forecasts(ctx, today) if f.needs_refill],
        suggestions=(
            care.rhythm_suggestions(db, ctx, moment) if allowed(Permission.MANAGE_ROUTINE) else []
        ),
        pending=[
            (request_, consent.describe_request(db, profile.id, request_)) for request_ in pending
        ],
        names=names,
        spoken=_spoken_summary(profile.elder_name, member.name, taken, doses_today, next_dose),
    )


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
    return redirect(url_for("today.show") + f"#dose-{slot_id}")


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


def _greeting(local_now: datetime) -> str:
    if local_now.hour < 12:
        return "Good morning"
    return "Good afternoon" if local_now.hour < 17 else "Good evening"


def _spoken_summary(
    elder_name: str,
    member_name: str,
    taken: int,
    doses_today: list[DoseView],
    next_dose: DoseView | None,
) -> str:
    """A short summary for the "Read aloud" button."""
    whose = "You have" if member_name == elder_name else f"{elder_name} has"
    if not doses_today:
        parts = ["There are no medicines scheduled for today."]
    else:
        parts = [f"{whose} taken {taken} of {len(doses_today)} doses today."]
    if next_dose is not None:
        med = next_dose.occurrence.medication
        when = friendly_clock(next_dose.occurrence.scheduled_at)
        parts.append(f"Next is {med.name} {med.dose}, {next_dose.when_label.lower()}, at {when}.")
    elif doses_today:
        parts.append("Nothing more is due today.")
    return " ".join(parts)
