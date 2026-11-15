"""Command-line helpers: ``flask --app hearth <command>``."""

from __future__ import annotations

import click
from flask import Flask, current_app

from . import scheduler
from .db import get_db
from .services import demo
from .services.passcodes import format_circle_code


def register(app: Flask) -> None:
    @app.cli.command("send-reminders")
    def send_reminders() -> None:
        """Run the reminder job once and report what went out."""
        count = scheduler.run_once(current_app)
        click.echo(f"{count} new notification(s).")

    @app.cli.command("seed-demo")
    def seed_demo() -> None:
        """Load the example care circle (only if it is not already there)."""
        config = current_app.config
        created = demo.seed_if_missing(get_db(), config["CLOCK"](), config["PASSCODE_HASH_METHOD"])
        code = format_circle_code(demo.DEMO_CIRCLE_CODE)
        if created:
            click.echo(f"Demo circle created. Circle code: {code}")
        else:
            click.echo(f"The demo circle already exists. Circle code: {code}")
        click.echo("Passcodes are listed in the README.")
