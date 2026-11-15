"""Care Weather, the journal, the email outbox and the change history."""

from __future__ import annotations

from datetime import timedelta

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

from ..access import Permission
from ..db import get_db
from ..engine.insights import by_day, by_medication
from ..repositories import profiles, records
from ..services import alerts, care, reminders
from ..services.mailer import mailer_from_config
from . import forms
from .security import allowed, current_member, current_profile, now, require

bp = Blueprint("insights", __name__)


@bp.get("/insights")
@require(Permission.VIEW_CARE)
def index() -> str:
    db = get_db()
    profile = current_profile()
    moment = now()
    ctx = care.load_context(db, profile)
    today = ctx.local(moment).date()
    history = care.outcomes_between(
        db, ctx, today - timedelta(days=care.PATTERN_DAYS - 1), today, moment
    )
    week_start = today - timedelta(days=care.WEEK_DAYS - 1)
    week = [record for record in history if record.scheduled_at.date() >= week_start]
    return render_template(
        "insights.html",
        report=care.care_weather_report(db, ctx, moment),
        days=by_day(week),
        per_medicine=by_medication(history),
        refills=care.refill_forecasts(ctx, today),
        pattern_days=care.PATTERN_DAYS,
    )


@bp.route("/journal", methods=["GET", "POST"])
@require(Permission.VIEW_CARE)
def journal() -> Response | str | tuple[str, int]:
    db = get_db()
    profile = current_profile()
    errors: forms.Errors = {}
    status = 200
    if request.method == "POST":
        if not allowed(Permission.WRITE_JOURNAL):
            return redirect(url_for("insights.journal"))
        entry, errors = forms.validate_journal(request.form)
        if entry is not None:
            kind, body = entry
            records.add_journal_entry(
                db,
                profile_id=profile.id,
                member_id=current_member().id,
                kind=kind,
                body=body,
                now=now(),
            )
            flash("Saved. It will appear on the next doctor visit summary.", "success")
            return redirect(url_for("insights.journal"))
        status = 422
    page = render_template(
        "journal.html",
        entries=records.list_journal(db, profile.id),
        names=profiles.member_names(db, profile.id),
        values=request.form if errors else {},
        errors=errors,
        can_write=allowed(Permission.WRITE_JOURNAL),
    )
    return (page, status) if status != 200 else page


@bp.get("/outbox")
@require(Permission.VIEW_ACTIVITY)
def outbox() -> str:
    return render_template(
        "outbox.html",
        notifications=records.list_notifications(get_db(), current_profile().id),
        sending_for_real=mailer_from_config(current_app.config) is not None,
    )


@bp.post("/outbox/check")
@require(Permission.VIEW_ACTIVITY)
def check_now() -> Response:
    """Run the reminder check for this circle straight away."""
    config = current_app.config
    count = reminders.run_profile(
        get_db(), mailer_from_config(config), current_profile(), now(), config["PUBLIC_BASE_URL"]
    )
    if count:
        flash(f"{count} new reminder{'s' if count != 1 else ''} went out.", "success")
    else:
        flash("Nothing new is due right now.", "info")
    return redirect(url_for("insights.outbox"))


@bp.post("/demo/test-alert")
@require(Permission.VIEW_ACTIVITY)
def test_alert() -> Response:
    """Demo only: send a marked test reminder so the alert flow can be seen at once."""
    config = current_app.config
    if not config["DEMO_MODE"]:
        abort(404)
    kind = request.form.get("kind", "")
    if kind not in alerts.TEST_KINDS:
        abort(400)
    count = alerts.send_test_alert(
        get_db(),
        mailer_from_config(config),
        current_profile(),
        kind,
        now(),
        config["PUBLIC_BASE_URL"],
    )
    if count:
        flash(f"Test sent: {count} email{'s' if count != 1 else ''}, shown below.", "success")
    else:
        flash("There is no dose today to base a test on, or no one to send it to.", "info")
    return redirect(url_for("insights.outbox"))


@bp.get("/history")
@require(Permission.VIEW_ACTIVITY)
def history() -> str:
    db = get_db()
    profile = current_profile()
    return render_template(
        "history.html",
        entries=records.list_audit(db, profile.id),
        names=profiles.member_names(db, profile.id),
    )
