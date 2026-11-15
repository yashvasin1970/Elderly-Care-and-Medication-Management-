"""Hearth: a care circle the elder runs.

Create the application with ``create_app()``. The package is laid out in
layers, each only depending on the ones below it:

* ``engine``        pure decision logic (schedules, alerts, insights)
* ``repositories``  SQL, scoped to one care profile at a time
* ``services``      use cases that combine the two
* ``web``           pages, forms and browser security
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import cli, db, scheduler, web
from .config import load_config
from .services import demo

__version__ = "1.0.0"


def create_app(overrides: Mapping[str, Any] | None = None) -> Flask:
    """Build and configure the Flask application."""
    app = Flask(__name__)
    app.config.update(load_config())
    if overrides:
        app.config.update(overrides)
    if not app.config["DATABASE"]:
        app.config["DATABASE"] = str(Path(app.instance_path) / "hearth.sqlite3")
    if app.config["TRUST_PROXY"]:
        # Behind a load balancer (e.g. Cloud Run), take the visitor's address
        # and scheme from the one proxy in front of us.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # type: ignore[method-assign]

    db.init_app(app)
    web.register(app)
    cli.register(app)

    if app.config["DEMO_MODE"]:
        with app.app_context():
            demo.seed_if_empty(
                db.get_db(),
                app.config["CLOCK"](),
                app.config["PASSCODE_HASH_METHOD"],
                app.config["PUBLIC_BASE_URL"],
            )
    if app.config["SECRET_KEY_IS_TEMPORARY"] and not app.testing:
        app.logger.warning(
            "HEARTH_SECRET_KEY is not set: using a temporary key, so everyone is"
            " signed out when the app restarts."
        )
    scheduler.start(app)
    return app
