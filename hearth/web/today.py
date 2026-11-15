"""The Today screen: the elder's day at a glance, and recording doses.

The page answers one question first, "is there anything to take right now?",
and only then shows the rest of the day, grouped into morning, afternoon,
evening and night.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ..access import Permission
from ..db import get_db
from ..engine.models import DoseStatus
from ..engine.schedule import PARTS_OF_DAY, part_of_day
from ..repositories import appointments, medications, profiles
from ..services import care, consent
from ..services.care import DoseView
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


@dataclass(frozen=True, slots=True)
class DayPart:
    """The doses that fall in one part of the day."""

    key: str
    label: str
    doses: list[DoseView]


@bp.get("/today")
@require(Permission.VIEW_CARE)
def show() -> str:
    db = get_db()
    profile = current_profile()
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
    take_now = carried_over + [v for v in doses_today if v.status in _OPEN]
    rest_of_day = [v for v in doses_today if v.status not in _OPEN]
    next_dose = next((v for v in doses_today if v.status is DoseStatus.UPCOMING), None)
    taken = sum(1 for view in doses_today if view.status is DoseStatus.TAKEN)

    is_owner = allowed(Permission.DECIDE_CHANGES)
    pending = medications.list_requests(db, profile.id, status="pending") if is_owner else []

    return render_template(
        "today.html",
        greeting=_greeting(local_now),
        local_now=local_now,
        total=len(doses_today),
        taken=taken,
        take_now=take_now,
        next_dose=next_dose,
        day_parts=group_by_part_of_day(rest_of_day),
        visits=appointments.list_between(db, profile.id, moment, moment + timedelta(days=7)),
        refills=[f for f in care.refill_forecasts(ctx, today) if f.needs_refill],
        suggestions=(
            care.rhythm_suggestions(db, ctx, moment) if allowed(Permission.MANAGE_ROUTINE) else []
        ),
        pending=[
            (request_, consent.describe_request(db, profile.id, request_)) for request_ in pending
        ],
        names=profiles.member_names(db, profile.id),
        spoken=spoken_summary(
            whose="You have" if is_owner else f"{profile.elder_name} has",
            taken=taken,
            total=len(doses_today),
            take_now=take_now,
            next_dose=next_dose,
        ),
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


def group_by_part_of_day(views: Sequence[DoseView]) -> list[DayPart]:
    """Group doses into morning, afternoon, evening and night, leaving out empty parts."""
    groups: dict[str, list[DoseView]] = {name: [] for name, _ in PARTS_OF_DAY}
    for view in views:
        groups[part_of_day(view.occurrence.scheduled_at)].append(view)
    return [
        DayPart(name, name.capitalize(), groups[name]) for name, _ in PARTS_OF_DAY if groups[name]
    ]


def spoken_summary(
    *,
    whose: str,
    taken: int,
    total: int,
    take_now: Sequence[DoseView],
    next_dose: DoseView | None,
) -> str:
    """A short, plain summary for the "Read aloud" button."""
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
