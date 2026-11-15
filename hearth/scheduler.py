"""An optional in-process timer that runs the reminder job.

Handy for running Hearth on one machine. In the cloud, leave this off and
have a scheduler call ``POST /tasks/reminders`` instead; both paths run the
same idempotent job, so using both by accident does no harm.
"""

from __future__ import annotations

import os
import threading

from flask import Flask

from .db import get_db
from .services import reminders
from .services.mailer import mailer_from_config


def run_once(app: Flask) -> int:
    """Run the reminder job once inside an application context."""
    with app.app_context():
        config = app.config
        return reminders.run(
            get_db(), mailer_from_config(config), config["CLOCK"](), config["PUBLIC_BASE_URL"]
        )


def start(app: Flask) -> threading.Thread | None:
    """Start the background timer if it is switched on."""
    if not app.config["RUN_SCHEDULER"] or app.testing:
        return None
    # Flask's reloader starts the app twice; only the serving process should tick.
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return None

    interval = app.config["SCHEDULER_INTERVAL_SECONDS"]
    stop = threading.Event()

    def tick() -> None:
        while not stop.wait(interval):
            try:
                run_once(app)
            except Exception:  # noqa: BLE001 - one bad run must not kill the timer
                app.logger.exception("The reminder job failed; it will retry on the next tick.")

    thread = threading.Thread(target=tick, name="hearth-reminders", daemon=True)
    thread.start()
    app.logger.info("Reminder timer started: checking every %s seconds.", interval)
    return thread
