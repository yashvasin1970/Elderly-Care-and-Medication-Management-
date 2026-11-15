"""Shared fixtures for the database, service and web tests.

Each test gets its own SQLite file and a clock pinned to a known moment, so
nothing depends on when the tests are run.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from hearth import create_app
from hearth.access import Role
from hearth.db import get_db
from hearth.repositories import profiles
from hearth.repositories.medications import MedicationInput, SlotInput
from hearth.services import consent, passcodes

# Monday 16 November 2026, 09:00 in Kolkata (03:30 UTC).
START = datetime(2026, 11, 16, 3, 30, tzinfo=UTC)
CIRCLE_CODE = "TEST2345"
# A fast hash keeps the suite quick; production uses scrypt.
FAST_HASH = "pbkdf2:sha256:1000"

PASSCODES = {
    Role.ELDER: "elder-pass-1",
    Role.CARETAKER: "carer-pass-2",
    Role.FAMILY: "family-pass-3",
    Role.VIEWER: "viewer-pass-4",
}


@dataclass
class Clock:
    """A clock tests can move forward by hand."""

    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class Circle:
    """IDs of the test circle and its members."""

    profile_id: int
    members: dict[Role, int] = field(default_factory=dict)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def app(tmp_path, clock: Clock) -> Iterator[Flask]:
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "SECRET_KEY": "test-secret",
            "SECRET_KEY_IS_TEMPORARY": False,
            "CLOCK": clock,
            "PASSCODE_HASH_METHOD": FAST_HASH,
            "DEMO_MODE": False,
            "SMTP_HOST": None,
            "GEMINI_API_KEY": None,
            "TASK_TOKEN": "task-token-for-tests",
            "PUBLIC_BASE_URL": "https://hearth.example",
        }
    )
    yield app


@pytest.fixture
def circle(app: Flask, clock: Clock) -> Circle:
    """A circle with one member of every role."""
    with app.app_context():
        db = get_db()
        profile_id = profiles.create_profile(
            db,
            circle_code=CIRCLE_CODE,
            elder_name="Kamala",
            timezone="Asia/Kolkata",
            now=clock.now - timedelta(days=30),
        )
        result = Circle(profile_id)
        people = {
            Role.ELDER: ("Kamala", "kamala@example.com"),
            Role.CARETAKER: ("Joseph", "joseph@example.com"),
            Role.FAMILY: ("Priya", "priya@example.com"),
            Role.VIEWER: ("Dr. Mehta", ""),
        }
        for role, (name, email) in people.items():
            result.members[role] = profiles.add_member(
                db,
                profile_id=profile_id,
                name=name,
                email=email,
                role=role,
                passcode_hash=passcodes.hash_passcode(PASSCODES[role], FAST_HASH),
                receives_alerts=role is not Role.VIEWER,
                now=clock.now - timedelta(days=30),
            )
    return result


def medicine(
    name: str = "Metformin", criticality: str = "important", **extra: Any
) -> MedicationInput:
    """A valid medicine input with sensible defaults."""
    values: dict[str, Any] = {
        "name": name,
        "dose": "500 mg",
        "criticality": criticality,
        "instructions": "",
        "missed_dose_rule": "",
        "pill_color": "white",
        "pill_shape": "round",
        "stock_count": None,
        "units_per_dose": 1,
        "start_date": "2026-10-01",
        "end_date": None,
        "days_mask": "1111111",
        "slots": (SlotInput("breakfast", 0),),
    }
    values.update(extra)
    return MedicationInput(**values)


def add_medicine(app: Flask, circle: Circle, data: MedicationInput, *, when: datetime) -> None:
    """Add a medicine as the elder (so it applies immediately)."""
    with app.app_context():
        db = get_db()
        elder = profiles.get_member(db, circle.profile_id, circle.members[Role.ELDER])
        assert elder is not None
        consent.submit_change(
            db, elder, action="create", medication_id=None, payload=data, reason="", now=when
        )


_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


class Browser:
    """A test client that handles CSRF tokens the way a real browser form would."""

    def __init__(self, client: FlaskClient) -> None:
        self.client = client

    def token(self) -> str:
        page = self.client.get("/").get_data(as_text=True)
        match = _CSRF.search(page)
        if match is None:  # signed in: the home page redirects, so use another page
            match = _CSRF.search(self.client.get("/today").get_data(as_text=True))
        assert match is not None, "no CSRF token found"
        return match.group(1)

    def get(self, path: str, **kwargs: Any):
        return self.client.get(path, **kwargs)

    def post(self, path: str, data: dict[str, Any] | None = None, **kwargs: Any):
        payload = {"csrf_token": self.token(), **(data or {})}
        return self.client.post(path, data=payload, **kwargs)

    def sign_in(self, passcode: str, code: str = CIRCLE_CODE):
        return self.post("/sign-in", {"circle_code": code, "passcode": passcode})


@pytest.fixture
def browser(app: Flask) -> Browser:
    return Browser(app.test_client())


@pytest.fixture
def as_role(app: Flask, circle: Circle):
    """Return a signed-in browser for a given role."""

    def make(role: Role) -> Browser:
        browser = Browser(app.test_client())
        response = browser.sign_in(PASSCODES[role])
        assert response.status_code == 302, response.get_data(as_text=True)
        return browser

    return make
