"""The care circle: who is in it, the daily routine, and the elder's settings.

Everything on these pages that changes something is reserved for the elder.
"""

from __future__ import annotations

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

from ..access import ASSIGNABLE_ROLES, ROLE_DESCRIPTIONS, ROLE_LABELS, Permission, Role
from ..db import get_db, transaction
from ..engine.models import ANCHORS
from ..repositories import profiles, records
from ..repositories.profiles import Member
from ..services import care, passcodes
from ..timeutil import format_clock, timezone_names
from . import forms, security
from .security import allowed, current_member, current_profile, now, require

bp = Blueprint("circle", __name__)

_ANCHOR_LABELS = {
    "wake": "I wake up at",
    "breakfast": "I have breakfast at",
    "lunch": "I have lunch at",
    "dinner": "I have dinner at",
    "bedtime": "I go to bed at",
}


# --- members --------------------------------------------------------------


@bp.get("/circle")
@require(Permission.VIEW_CARE)
def index() -> str:
    return render_template(
        "circle.html",
        members=profiles.list_members(get_db(), current_profile().id),
        can_manage=allowed(Permission.MANAGE_CIRCLE),
        role_labels=ROLE_LABELS,
        role_descriptions=ROLE_DESCRIPTIONS,
    )


@bp.route("/circle/add", methods=["GET", "POST"])
@require(Permission.MANAGE_CIRCLE)
def add() -> Response | str | tuple[str, int]:
    if request.method == "GET":
        return _member_form({"role": Role.FAMILY.value, "receives_alerts": "1"}, {}, None)

    db = get_db()
    profile = current_profile()
    data, errors = forms.validate_member(request.form)
    passcode = request.form.get("passcode", "").strip() or passcodes.generate_passcode()
    problem = passcodes.passcode_problem(passcode)
    if problem:
        errors["passcode"] = problem
    elif passcodes.is_passcode_taken(db, profile.id, passcode):
        errors["passcode"] = "Someone in the circle already uses that passcode. Choose another."
    if data is None or errors:
        return _member_form(request.form, errors, None), 422

    moment = now()
    with transaction(db):
        profiles.add_member(
            db,
            profile_id=profile.id,
            name=data.name,
            email=data.email,
            role=data.role,
            passcode_hash=passcodes.hash_passcode(passcode, _hash_method()),
            trusted_editor=data.trusted_editor,
            receives_alerts=data.receives_alerts,
            now=moment,
        )
        _audit("Added someone to the circle", f"{data.name} ({ROLE_LABELS[data.role]})")
    # Shown once, here, and never stored in readable form.
    return render_template("passcode.html", person=data.name, passcode=passcode, is_new=True)


@bp.route("/circle/<int:member_id>/edit", methods=["GET", "POST"])
@require(Permission.MANAGE_CIRCLE)
def edit(member_id: int) -> Response | str | tuple[str, int]:
    member = _other_member(member_id)
    if request.method == "GET":
        values = {"name": member.name, "email": member.email, "role": member.role.value}
        if member.trusted_editor:
            values["trusted_editor"] = "1"
        if member.receives_alerts:
            values["receives_alerts"] = "1"
        return _member_form(values, {}, member)

    data, errors = forms.validate_member(request.form)
    if data is None:
        return _member_form(request.form, errors, member), 422
    profiles.update_member(
        get_db(),
        current_profile().id,
        member_id,
        name=data.name,
        email=data.email,
        role=data.role,
        trusted_editor=data.trusted_editor,
        receives_alerts=data.receives_alerts,
    )
    _audit("Changed someone's access", f"{data.name} ({ROLE_LABELS[data.role]})")
    flash(f"{data.name}'s details have been updated.", "success")
    return redirect(url_for("circle.index"))


@bp.post("/circle/<int:member_id>/passcode")
@require(Permission.MANAGE_CIRCLE)
def new_passcode(member_id: int) -> str:
    """Give a member a new passcode, signing them out everywhere."""
    member = _other_member(member_id)
    db = get_db()
    profile = current_profile()
    passcode = passcodes.generate_passcode()
    while passcodes.is_passcode_taken(db, profile.id, passcode):
        passcode = passcodes.generate_passcode()
    profiles.set_passcode(
        db, profile.id, member_id, passcodes.hash_passcode(passcode, _hash_method())
    )
    _audit("Gave someone a new passcode", member.name)
    return render_template("passcode.html", person=member.name, passcode=passcode, is_new=False)


@bp.post("/circle/<int:member_id>/remove")
@require(Permission.MANAGE_CIRCLE)
def remove(member_id: int) -> Response:
    member = _other_member(member_id)
    profiles.deactivate_member(get_db(), current_profile().id, member_id)
    _audit("Removed someone from the circle", member.name)
    flash(f"{member.name} no longer has access.", "success")
    return redirect(url_for("circle.index"))


# --- routine --------------------------------------------------------------


@bp.route("/routine", methods=["GET", "POST"])
@require(Permission.VIEW_CARE)
def routine() -> Response | str | tuple[str, int]:
    profile = current_profile()
    if request.method == "GET":
        values = {anchor: format_clock(profile.routine[anchor]) for anchor in ANCHORS}
        return _routine_form(values, {})
    if not allowed(Permission.MANAGE_ROUTINE):
        abort(403)
    data, errors = forms.validate_routine(request.form)
    if data is None:
        return _routine_form(request.form, errors), 422
    profiles.update_routine(get_db(), profile.id, data)
    _audit("Changed the daily routine", ", ".join(f"{a} {format_clock(data[a])}" for a in ANCHORS))
    flash("Your routine is saved. Reminders tied to it have moved with it.", "success")
    return redirect(url_for("circle.routine"))


def _routine_form(values: forms.Form, errors: forms.Errors) -> str:
    db = get_db()
    ctx = care.load_context(db, current_profile())
    return render_template(
        "routine.html",
        values=values,
        errors=errors,
        anchors=[(anchor, _ANCHOR_LABELS[anchor]) for anchor in ANCHORS],
        suggestions=care.rhythm_suggestions(db, ctx, now()),
        can_manage=allowed(Permission.MANAGE_ROUTINE),
    )


# --- settings -------------------------------------------------------------


@bp.route("/settings", methods=["GET", "POST"])
@require(Permission.MANAGE_SETTINGS)
def settings() -> Response | str | tuple[str, int]:
    profile = current_profile()
    member = current_member()
    if request.method == "GET":
        values = {
            "elder_name": profile.elder_name,
            "email": member.email,
            "timezone": profile.timezone,
            "checkin_hour": str(profile.checkin_hour),
        }
        if profile.checkin_enabled:
            values["checkin_enabled"] = "1"
        if member.receives_alerts:
            values["receives_alerts"] = "1"
        return _settings_form(values, {})

    data, errors = forms.validate_settings(request.form)
    if data is None:
        return _settings_form(request.form, errors), 422
    db = get_db()
    with transaction(db):
        profiles.update_settings(
            db,
            profile.id,
            elder_name=data.elder_name,
            timezone=data.timezone,
            checkin_enabled=data.checkin_enabled,
            checkin_hour=data.checkin_hour,
        )
        profiles.update_member(
            db,
            profile.id,
            member.id,
            name=data.elder_name,
            email=data.email,
            role=Role.ELDER,
            trusted_editor=False,
            receives_alerts=data.receives_alerts,
        )
        _audit("Changed the settings")
    flash("Your settings are saved.", "success")
    return redirect(url_for("circle.settings"))


@bp.post("/settings/passcode")
@require(Permission.MANAGE_SETTINGS)
def change_own_passcode() -> Response | tuple[str, int]:
    """Let the elder change their own passcode, after confirming the old one."""
    db = get_db()
    profile = current_profile()
    member = current_member()
    current = request.form.get("current_passcode", "")
    new = request.form.get("new_passcode", "")
    errors: forms.Errors = {}
    if passcodes.find_member_id(db, profile.id, current) != member.id:
        errors["current_passcode"] = "That is not your current passcode."
    problem = passcodes.passcode_problem(new)
    if problem:
        errors["new_passcode"] = problem
    elif new != request.form.get("new_passcode_again", ""):
        errors["new_passcode_again"] = "The two new passcodes are not the same."
    elif passcodes.is_passcode_taken(db, profile.id, new):
        errors["new_passcode"] = "Someone in the circle already uses that passcode."
    if errors:
        values = {
            "elder_name": profile.elder_name,
            "email": member.email,
            "timezone": profile.timezone,
            "checkin_hour": str(profile.checkin_hour),
            "checkin_enabled": "1" if profile.checkin_enabled else "",
            "receives_alerts": "1" if member.receives_alerts else "",
        }
        return _settings_form(values, errors), 422
    profiles.set_passcode(db, profile.id, member.id, passcodes.hash_passcode(new, _hash_method()))
    _audit("Changed their own passcode")
    # The old sessions are now invalid, so start a new one for this browser.
    refreshed = profiles.get_member(db, profile.id, member.id)
    if refreshed is not None:
        security.sign_in(refreshed)
    flash("Your passcode has been changed.", "success")
    return redirect(url_for("circle.settings"))


def _settings_form(values: forms.Form, errors: forms.Errors) -> str:
    return render_template(
        "settings.html", values=values, errors=errors, timezones=timezone_names()
    )


# --- helpers --------------------------------------------------------------


def _other_member(member_id: int) -> Member:
    """Find a member the elder may manage: anyone in the circle but themself."""
    member = profiles.get_member(get_db(), current_profile().id, member_id)
    if member is None or member.role is Role.ELDER:
        abort(404)
    return member


def _member_form(values: forms.Form, errors: forms.Errors, member: Member | None) -> str:
    return render_template(
        "member_form.html",
        values=values,
        errors=errors,
        editing=member,
        roles=[(role, ROLE_LABELS[role], ROLE_DESCRIPTIONS[role]) for role in ASSIGNABLE_ROLES],
    )


def _hash_method() -> str:
    return current_app.config["PASSCODE_HASH_METHOD"]


def _audit(action: str, detail: str = "") -> None:
    records.add_audit(
        get_db(),
        profile_id=current_profile().id,
        member_id=current_member().id,
        action=action,
        detail=detail,
        now=now(),
    )
