"""The medicine list, the add/edit form, and requests awaiting the elder."""

from __future__ import annotations

from dataclasses import dataclass

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

from ..access import Permission, requires_elder_approval
from ..db import get_db
from ..engine.models import FIXED, Medication
from ..engine.refill import RefillForecast
from ..engine.schedule import describe_slot, slot_time
from ..repositories import medications, profiles, records
from ..services import care, consent, gemini, throttle
from ..services.consent import ChangeOutcome
from ..timeutil import friendly_clock
from . import forms
from .security import allowed, current_member, current_profile, now, require

bp = Blueprint("medications", __name__)


@dataclass(frozen=True, slots=True)
class MedicineCard:
    """A medicine with its schedule and stock, ready to display."""

    medication: Medication
    times: list[str]
    days: str
    forecast: RefillForecast | None


@bp.get("/medicines")
@require(Permission.VIEW_CARE)
def index() -> str:
    db = get_db()
    profile = current_profile()
    ctx = care.load_context(db, profile)
    today = ctx.local(now()).date()
    forecasts = {f.medication_id: f for f in care.refill_forecasts(ctx, today)}

    cards, stopped = [], []
    for med in sorted(ctx.medications.values(), key=lambda m: m.name.lower()):
        if not med.active:
            stopped.append(med)
            continue
        slots = ctx.active_slots(med.id)
        times = []
        for slot in sorted(slots, key=lambda s: slot_time(s, profile.routine)):
            clock = friendly_clock(slot_time(slot, profile.routine))
            label = "" if slot.anchor == FIXED else describe_slot(slot).lower()
            times.append(f"{clock} · {label}" if label else clock)
        days = consent.days_label(slots[0].days_mask) if slots else ""
        cards.append(MedicineCard(med, times, days, forecasts.get(med.id)))

    return render_template(
        "medicines.html",
        cards=cards,
        stopped=stopped,
        needs_approval=_needs_approval(),
        ai_enabled=bool(current_app.config.get("GEMINI_API_KEY")),
    )


@bp.route("/medicines/new", methods=["GET", "POST"])
@require(Permission.PROPOSE_MEDICATION_CHANGES)
def new() -> Response | str | tuple[str, int]:
    db = get_db()
    today = care.load_context(db, current_profile()).local(now()).date()
    if request.method == "GET":
        return _form(forms.blank_medication_values(today), {})

    data, errors = forms.validate_medication(request.form, today)
    if data is None:
        return _form(request.form, errors), 422
    outcome = consent.submit_change(
        db,
        current_member(),
        action="create",
        medication_id=None,
        payload=data,
        reason=forms.clean_line(request.form, "reason", 200),
        now=now(),
    )
    _flash_outcome(outcome, f"{data.name} has been added.")
    return redirect(url_for("medications.index"))


@bp.post("/medicines/draft")
@require(Permission.PROPOSE_MEDICATION_CHANGES)
def draft() -> Response | str:
    """Pre-fill the form from a sentence. Nothing is saved at this point."""
    text = forms.clean_line(request.form, "sentence", gemini.MAX_TEXT_LENGTH)
    if not text:
        flash("Type a sentence about the medicine first.", "error")
        return redirect(url_for("medications.index"))
    db = get_db()
    moment = now()
    bucket = f"smart-add:{current_member().id}"
    use_ai = throttle.locked_until(db, bucket, moment) is None
    if use_ai:
        throttle.record_attempt(db, bucket, throttle.SMART_ADD_PER_MEMBER, moment)
    result = gemini.draft_medication(text, current_app.config, use_ai=use_ai)
    today = care.load_context(db, current_profile()).local(moment).date()
    helper = "Gemini" if result.source == "gemini" else "Hearth"
    notice = (
        f"{helper} filled this in from your sentence. Nothing is saved yet:"
        " please check every field against the label before you save."
    )
    return _form(forms.draft_form_values(result, today), {}, notice=notice)


@bp.route("/medicines/<int:medication_id>/edit", methods=["GET", "POST"])
@require(Permission.PROPOSE_MEDICATION_CHANGES)
def edit(medication_id: int) -> Response | str | tuple[str, int]:
    db = get_db()
    profile = current_profile()
    current = medications.get_input(db, profile.id, medication_id)
    medication = medications.get_medication(db, profile.id, medication_id)
    if current is None or medication is None or not medication.active:
        abort(404)
    if request.method == "GET":
        return _form(forms.medication_form_values(current), {}, medication_id=medication_id)

    today = care.load_context(db, profile).local(now()).date()
    data, errors = forms.validate_medication(request.form, today)
    if data is None:
        return _form(request.form, errors, medication_id=medication_id), 422
    if data == current:
        flash("Nothing was changed.", "info")
        return redirect(url_for("medications.index"))
    outcome = consent.submit_change(
        db,
        current_member(),
        action="update",
        medication_id=medication_id,
        payload=data,
        reason=forms.clean_line(request.form, "reason", 200),
        now=now(),
    )
    _flash_outcome(outcome, f"{data.name} has been updated.")
    return redirect(url_for("medications.index"))


@bp.post("/medicines/<int:medication_id>/stop")
@require(Permission.PROPOSE_MEDICATION_CHANGES)
def stop(medication_id: int) -> Response:
    db = get_db()
    medication = medications.get_medication(db, current_profile().id, medication_id)
    if medication is None or not medication.active:
        abort(404)
    outcome = consent.submit_change(
        db,
        current_member(),
        action="archive",
        medication_id=medication_id,
        payload=None,
        reason=forms.clean_line(request.form, "reason", 200),
        now=now(),
    )
    _flash_outcome(outcome, f"{medication.name} has been stopped. Its history is kept.")
    return redirect(url_for("medications.index"))


@bp.post("/medicines/<int:medication_id>/restock")
@require(Permission.PROPOSE_MEDICATION_CHANGES)
def restock(medication_id: int) -> Response:
    db = get_db()
    profile = current_profile()
    medication = medications.get_medication(db, profile.id, medication_id)
    if medication is None or not medication.active:
        abort(404)
    try:
        amount = int(request.form.get("amount", ""))
    except ValueError:
        amount = 0
    if not 1 <= amount <= 999:
        flash("Enter how many were added, from 1 to 999.", "error")
    else:
        medications.restock(db, profile.id, medication_id, amount)
        records.add_audit(
            db,
            profile_id=profile.id,
            member_id=current_member().id,
            action="Added stock",
            detail=f"{medication.name}: {amount} added",
            now=now(),
        )
        flash(f"Added {amount} to the stock of {medication.name}.", "success")
    return redirect(url_for("medications.index"))


@bp.get("/requests")
@require(Permission.VIEW_CARE)
def requests_index() -> str:
    """Suggested changes: waiting for the elder, and recently decided."""
    db = get_db()
    profile = current_profile()
    names = profiles.member_names(db, profile.id)
    pending, decided = [], []
    for item in medications.list_requests(db, profile.id, limit=30):
        entry = {
            "request": item,
            "summary": consent.describe_request(db, profile.id, item),
            "proposer": names.get(item.proposed_by or 0, "Someone no longer in the circle"),
        }
        if item.status == "pending":
            current = (
                medications.get_input(db, profile.id, item.medication_id)
                if item.medication_id
                else None
            )
            entry["changes"] = consent.describe_changes(current, item.payload)
            pending.append(entry)
        else:
            decided.append(entry)
    return render_template(
        "requests.html",
        pending=pending,
        decided=decided,
        can_decide=allowed(Permission.DECIDE_CHANGES),
    )


@bp.post("/requests/<int:request_id>/<decision>")
@require(Permission.DECIDE_CHANGES)
def decide(request_id: int, decision: str) -> Response:
    if decision not in ("approve", "decline"):
        abort(404)
    approve = decision == "approve"
    if consent.decide(get_db(), current_member(), request_id, approve=approve, now=now()):
        flash(
            "Approved. The change has been made." if approve else "Declined. Nothing was changed.",
            "success",
        )
    else:
        flash("That request has already been decided.", "info")
    return redirect(url_for("medications.requests_index"))


def _needs_approval() -> bool:
    member = current_member()
    return requires_elder_approval(member.role, member.trusted_editor)


def _flash_outcome(outcome: ChangeOutcome, applied_message: str) -> None:
    if outcome is ChangeOutcome.PENDING:
        elder = current_profile().elder_name
        flash(
            f"Your suggestion has been sent to {elder}. Nothing changes until they approve it.",
            "info",
        )
    else:
        flash(applied_message, "success")


def _form(
    values: forms.Form,
    errors: forms.Errors,
    *,
    medication_id: int | None = None,
    notice: str | None = None,
) -> str:
    return render_template(
        "medicine_form.html",
        values=values,
        errors=errors,
        medication_id=medication_id,
        notice=notice,
        needs_approval=_needs_approval(),
        anchor_choices=forms.ANCHOR_CHOICES,
        offset_choices=forms.OFFSET_CHOICES,
        criticality_choices=forms.CRITICALITY_CHOICES,
        pill_colors=forms.PILL_COLORS,
        pill_shapes=forms.PILL_SHAPES,
        day_names=forms.DAY_NAMES,
        max_slots=forms.MAX_SLOTS,
    )
