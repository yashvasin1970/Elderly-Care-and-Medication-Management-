"""Settings, read from environment variables.

Nothing secret is ever hard-coded. See ``.env.example`` for every setting.
"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from typing import Any

from .services.passcodes import DEFAULT_HASH_METHOD
from .timeutil import utc_now

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in _TRUE_VALUES


def _number(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def load_config() -> dict[str, Any]:
    """Build the Flask configuration from the environment."""
    secret_key = os.environ.get("HEARTH_SECRET_KEY")
    return {
        # Sessions are signed with this key. Without one we generate a random
        # key per start, which is safe but signs everyone out on restart.
        "SECRET_KEY": secret_key or secrets.token_hex(32),
        "SECRET_KEY_IS_TEMPORARY": secret_key is None,
        "DATABASE": os.environ.get("HEARTH_DATABASE"),
        "PUBLIC_BASE_URL": os.environ.get("HEARTH_BASE_URL", "http://localhost:5000"),
        # Cookies: never readable by scripts, never sent cross-site.
        "SESSION_COOKIE_NAME": "hearth_session",
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": _flag("HEARTH_SECURE_COOKIES"),
        # Set when running behind exactly one trusted proxy or load balancer.
        "TRUST_PROXY": _flag("HEARTH_TRUST_PROXY"),
        "PERMANENT_SESSION_LIFETIME": timedelta(hours=_number("HEARTH_SESSION_HOURS", 72)),
        # Forms are small; refuse anything larger outright.
        "MAX_CONTENT_LENGTH": 64 * 1024,
        "PASSCODE_HASH_METHOD": DEFAULT_HASH_METHOD,
        # Email. With no SMTP host, messages go to the in-app Outbox only.
        "SMTP_HOST": os.environ.get("SMTP_HOST"),
        "SMTP_PORT": _number("SMTP_PORT", 587),
        "SMTP_USERNAME": os.environ.get("SMTP_USERNAME"),
        "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD"),
        "SMTP_FROM": os.environ.get("SMTP_FROM"),
        "SMTP_USE_TLS": _flag("SMTP_USE_TLS", default=True),
        # Optional AI help for typing in a medicine.
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
        "GEMINI_MODEL": os.environ.get("GEMINI_MODEL"),
        # Reminder job: run in-process, and/or let a scheduler call the endpoint.
        "RUN_SCHEDULER": _flag("HEARTH_RUN_SCHEDULER"),
        "SCHEDULER_INTERVAL_SECONDS": max(15, _number("HEARTH_SCHEDULER_SECONDS", 60)),
        "TASK_TOKEN": os.environ.get("HEARTH_TASK_TOKEN"),
        # Load a ready-made example circle when the database is empty.
        "DEMO_MODE": _flag("HEARTH_DEMO_MODE"),
        # The clock is injectable so tests can pin "now".
        "CLOCK": utc_now,
    }
