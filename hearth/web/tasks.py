"""Machine endpoints: the scheduled reminder trigger and a health check."""

from __future__ import annotations

import hmac

from flask import Blueprint, Response, abort, current_app, jsonify, request

from ..db import get_db
from ..services import reminders
from ..services.mailer import mailer_from_config
from .security import now

bp = Blueprint("tasks", __name__)


@bp.post("/tasks/reminders")
def run_reminders() -> Response:
    """Run the reminder job. Meant to be called by a scheduler, e.g. Cloud Scheduler.

    The caller must present the shared task token. With no token configured
    the endpoint does not exist at all.
    """
    expected = current_app.config.get("TASK_TOKEN")
    if not expected:
        abort(404)
    header = request.headers.get("Authorization", "")
    presented = header.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        abort(403)
    config = current_app.config
    sent = reminders.run(get_db(), mailer_from_config(config), now(), config["PUBLIC_BASE_URL"])
    return jsonify(sent=sent)


@bp.get("/healthz")
def health() -> Response:
    return jsonify(status="ok")
