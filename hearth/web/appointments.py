"""Doctor appointments and the one-page visit summary."""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ..access import Permission
from ..db import get_db
from ..engine.insights import by_medication, summarise
from ..engine.models import FIXED, DoseStatus
from ..engine.schedule import describe_slot, slot_time
from ..repositories import appointments, profiles, records
from ..repositories.appointments import Appointment
from ..services import care, consent
from ..timeutil import friendly_clock
from . import forms
from .security import current_member, current_profile, now, require

bp = Blueprint("appointments", __name__)

#: With no earlier visit to measure from, the summary covers this many days.
_DEFAULT_SUMMARY_DAYS = 30
_MAX_SUMMARY_DAYS = 90


@bp.get("/appointments")
@require(Permission.VIEW_CARE)
def index() -> str:
    db = get_db()
    profile = current_profile()
    moment = now()
    return render_template(
        "appointments.html",
        upcoming=appointments.list_upcoming(db, profile.id, moment),
        past=appointments.list_past(db, profile.id, moment),
    )


@bp.route("/appointments/new", methods=["GET", "POST"])
@require(Permission.MANAGE_APPOINTMENTS)
def new() -> Response | str | tuple[str, int]:
    if request.method == "GET":
        return render_template("appointment_form.html", values={}, errors={}, appointment=None)
    db = get_db()
    profile = current_profile()
    ctx = care.load_context(db, profile)
    data, errors = forms.validate_appointment(request.form, ctx.tz)
    if data is None:
        return (
            render_template(
                "appointment_form.html", values=request.form, errors=errors, appointment=None
            ),
            422,
        )
    moment = now()
    appointments.create_appointment(
        db, profile.id, data, created_by=current_member().id, now=moment
    )
    _audit("Added an appointment", f"{data.title}, {ctx.local(data.starts_at):%d %b %Y %H:%M}")
    flash(f"{data.title} has been added. Reminders will go out the day before.", "success")
    return redirect(url_for("appointments.index"))


@bp.route("/appointments/<int:appointment_id>/edit", methods=["GET", "POST"])
@require(Permission.MANAGE_APPOINTMENTS)
def edit(appointment_id: int) -> Response | str | tuple[str, int]:
    db = get_db()
    profile = current_profile()
    appointment = _find(appointment_id)
    ctx = care.load_context(db, profile)
    if request.method == "GET":
        values = {
            "title": appointment.title,
            "doctor": appointment.doctor,
            "location": appointment.location,
            "notes": appointment.notes,
            "starts_at": ctx.local(appointment.starts_at).strftime("%Y-%m-%dT%H:%M"),
        }
        return render_template(
            "appointment_form.html", values=values, errors={}, appointment=appointment
        )
    data, errors = forms.validate_appointment(request.form, ctx.tz)
    if data is None:
        return (
            render_template(
                "appointment_form.html", values=request.form, errors=errors, appointment=appointment
            ),
            422,
        )
    appointments.update_appointment(db, profile.id, appointment_id, data)
    _audit("Changed an appointment", f"{data.title}, {ctx.local(data.starts_at):%d %b %Y %H:%M}")
    flash(f"{data.title} has been updated.", "success")
    return redirect(url_for("appointments.index"))


@bp.post("/appointments/<int:appointment_id>/cancel")
@require(Permission.MANAGE_APPOINTMENTS)
def cancel(appointment_id: int) -> Response:
    appointment = _find(appointment_id)
    appointments.cancel_appointment(get_db(), current_profile().id, appointment_id)
    _audit("Cancelled an appointment", appointment.title)
    flash(f"{appointment.title} has been cancelled.", "success")
    return redirect(url_for("appointments.index"))


@bp.get("/appointments/<int:appointment_id>/summary")
@require(Permission.VIEW_CARE)
def summary(appointment_id: int) -> str:
    """A printable page to take to the doctor.

    It covers the time since the previous visit: how medicines were taken,
    anything the elder noted, and the questions they want to ask.
    """
    db = get_db()
    profile = current_profile()
    appointment = _find(appointment_id)
    moment = now()
    ctx = care.load_context(db, profile)

    period_end = min(moment, appointment.starts_at)
    previous = appointments.previous_before(db, profile.id, appointment.starts_at)
    period_start = period_end - timedelta(days=_DEFAULT_SUMMARY_DAYS)
    if previous is not None and previous.starts_at > period_end - timedelta(days=_MAX_SUMMARY_DAYS):
        period_start = previous.starts_at

    outcomes = care.outcomes_between(
        db, ctx, ctx.local(period_start).date(), ctx.local(period_end).date(), moment
    )
    journal = records.list_journal(db, profile.id, since=period_start)
    medicines = []
    for med in sorted(ctx.medications.values(), key=lambda m: m.name.lower()):
        if not med.active:
            continue
        times = [
            friendly_clock(slot_time(slot, profile.routine))
            if slot.anchor == FIXED
            else f"{describe_slot(slot)} ({friendly_clock(slot_time(slot, profile.routine))})"
            for slot in ctx.active_slots(med.id)
        ]
        slots = ctx.active_slots(med.id)
        days = consent.days_label(slots[0].days_mask) if slots else ""
        medicines.append((med, times, days))

    return render_template(
        "visit_summary.html",
        appointment=appointment,
        previous=previous if previous and previous.starts_at == period_start else None,
        period_start=period_start,
        period_end=period_end,
        overall=summarise(outcomes),
        per_medicine=by_medication(outcomes),
        missed=[o for o in outcomes if o.status is DoseStatus.MISSED][-20:],
        medicines=medicines,
        questions=[entry for entry in journal if entry.kind == "question"],
        symptoms=[entry for entry in journal if entry.kind == "symptom"],
        notes=[entry for entry in journal if entry.kind == "note"],
        names=profiles.member_names(db, profile.id),
    )


def _find(appointment_id: int) -> Appointment:
    appointment = appointments.get_appointment(get_db(), current_profile().id, appointment_id)
    if appointment is None or appointment.cancelled:
        abort(404)
    return appointment


def _audit(action: str, detail: str) -> None:
    records.add_audit(
        get_db(),
        profile_id=current_profile().id,
        member_id=current_member().id,
        action=action,
        detail=detail,
        now=now(),
    )
