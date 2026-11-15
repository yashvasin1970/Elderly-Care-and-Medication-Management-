"""Care profiles (one per elder) and the members of each circle."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time

from ..access import Role
from ..engine.models import ANCHORS, DEFAULT_ROUTINE
from ..timeutil import format_clock, from_iso_or_none, parse_clock, to_iso


@dataclass(frozen=True, slots=True)
class Profile:
    """An elder's care profile."""

    id: int
    circle_code: str
    elder_name: str
    timezone: str
    routine: dict[str, time]
    checkin_enabled: bool
    checkin_hour: int
    last_elder_activity_at: datetime | None


@dataclass(frozen=True, slots=True)
class Member:
    """Someone with access to a care profile."""

    id: int
    profile_id: int
    name: str
    email: str
    role: Role
    trusted_editor: bool
    receives_alerts: bool
    session_version: int
    active: bool


# --- profiles -------------------------------------------------------------


def create_profile(
    db: sqlite3.Connection,
    *,
    circle_code: str,
    elder_name: str,
    timezone: str,
    now: datetime,
) -> int:
    cursor = db.execute(
        "INSERT INTO profiles (circle_code, elder_name, timezone, routine_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (circle_code, elder_name, timezone, _routine_to_json(DEFAULT_ROUTINE), to_iso(now)),
    )
    return int(cursor.lastrowid or 0)


def get_profile(db: sqlite3.Connection, profile_id: int) -> Profile | None:
    row = db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return _profile(row) if row else None


def get_profile_by_code(db: sqlite3.Connection, circle_code: str) -> Profile | None:
    row = db.execute("SELECT * FROM profiles WHERE circle_code = ?", (circle_code,)).fetchone()
    return _profile(row) if row else None


def list_profiles(db: sqlite3.Connection) -> list[Profile]:
    return [_profile(row) for row in db.execute("SELECT * FROM profiles ORDER BY id")]


def count_profiles(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0])


def update_settings(
    db: sqlite3.Connection,
    profile_id: int,
    *,
    elder_name: str,
    timezone: str,
    checkin_enabled: bool,
    checkin_hour: int,
) -> None:
    db.execute(
        "UPDATE profiles SET elder_name = ?, timezone = ?, checkin_enabled = ?, checkin_hour = ?"
        " WHERE id = ?",
        (elder_name, timezone, int(checkin_enabled), checkin_hour, profile_id),
    )


def update_routine(db: sqlite3.Connection, profile_id: int, routine: Mapping[str, time]) -> None:
    db.execute(
        "UPDATE profiles SET routine_json = ? WHERE id = ?",
        (_routine_to_json(routine), profile_id),
    )


def touch_elder_activity(db: sqlite3.Connection, profile_id: int, now: datetime) -> None:
    db.execute(
        "UPDATE profiles SET last_elder_activity_at = ? WHERE id = ?", (to_iso(now), profile_id)
    )


# --- dismissed suggestions ------------------------------------------------


def dismiss_suggestion(db: sqlite3.Connection, profile_id: int, key: str, now: datetime) -> None:
    db.execute(
        "INSERT OR IGNORE INTO dismissed_suggestions (profile_id, key, dismissed_at)"
        " VALUES (?, ?, ?)",
        (profile_id, key, to_iso(now)),
    )


def dismissed_suggestion_keys(db: sqlite3.Connection, profile_id: int) -> set[str]:
    rows = db.execute("SELECT key FROM dismissed_suggestions WHERE profile_id = ?", (profile_id,))
    return {row["key"] for row in rows}


# --- members --------------------------------------------------------------


def add_member(
    db: sqlite3.Connection,
    *,
    profile_id: int,
    name: str,
    email: str,
    role: Role,
    passcode_hash: str,
    trusted_editor: bool = False,
    receives_alerts: bool = True,
    now: datetime,
) -> int:
    cursor = db.execute(
        "INSERT INTO members (profile_id, name, email, role, passcode_hash, trusted_editor,"
        " receives_alerts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            profile_id,
            name,
            email,
            role.value,
            passcode_hash,
            int(trusted_editor),
            int(receives_alerts),
            to_iso(now),
        ),
    )
    return int(cursor.lastrowid or 0)


def get_member(db: sqlite3.Connection, profile_id: int, member_id: int) -> Member | None:
    """Fetch an active member of the given profile."""
    row = db.execute(
        "SELECT * FROM members WHERE id = ? AND profile_id = ? AND active = 1",
        (member_id, profile_id),
    ).fetchone()
    return _member(row) if row else None


def list_members(db: sqlite3.Connection, profile_id: int) -> list[Member]:
    """Active members, the elder first, then by name."""
    rows = db.execute(
        "SELECT * FROM members WHERE profile_id = ? AND active = 1"
        " ORDER BY role = 'elder' DESC, name COLLATE NOCASE",
        (profile_id,),
    )
    return [_member(row) for row in rows]


def member_names(db: sqlite3.Connection, profile_id: int) -> dict[int, str]:
    """Names of everyone who has ever been in the circle, for history views."""
    rows = db.execute("SELECT id, name FROM members WHERE profile_id = ?", (profile_id,))
    return {row["id"]: row["name"] for row in rows}


def passcode_hashes(db: sqlite3.Connection, profile_id: int) -> list[tuple[int, str]]:
    rows = db.execute(
        "SELECT id, passcode_hash FROM members WHERE profile_id = ? AND active = 1", (profile_id,)
    )
    return [(row["id"], row["passcode_hash"]) for row in rows]


def update_member(
    db: sqlite3.Connection,
    profile_id: int,
    member_id: int,
    *,
    name: str,
    email: str,
    role: Role,
    trusted_editor: bool,
    receives_alerts: bool,
) -> None:
    db.execute(
        "UPDATE members SET name = ?, email = ?, role = ?, trusted_editor = ?, receives_alerts = ?"
        " WHERE id = ? AND profile_id = ?",
        (name, email, role.value, int(trusted_editor), int(receives_alerts), member_id, profile_id),
    )


def set_passcode(
    db: sqlite3.Connection, profile_id: int, member_id: int, passcode_hash: str
) -> None:
    """Change a passcode and sign that member out everywhere."""
    db.execute(
        "UPDATE members SET passcode_hash = ?, session_version = session_version + 1"
        " WHERE id = ? AND profile_id = ?",
        (passcode_hash, member_id, profile_id),
    )


def deactivate_member(db: sqlite3.Connection, profile_id: int, member_id: int) -> None:
    """Remove a member from the circle; their open sessions stop working at once."""
    db.execute(
        "UPDATE members SET active = 0, session_version = session_version + 1"
        " WHERE id = ? AND profile_id = ?",
        (member_id, profile_id),
    )


# --- row mapping ----------------------------------------------------------


def _routine_to_json(routine: Mapping[str, time]) -> str:
    return json.dumps({anchor: format_clock(routine[anchor]) for anchor in ANCHORS})


def _routine_from_json(raw: str) -> dict[str, time]:
    stored = json.loads(raw)
    routine = dict(DEFAULT_ROUTINE)
    for anchor in ANCHORS:
        try:
            routine[anchor] = parse_clock(stored[anchor])
        except (KeyError, TypeError, ValueError):
            continue  # keep the default for anything missing or malformed
    return routine


def _profile(row: sqlite3.Row) -> Profile:
    return Profile(
        id=row["id"],
        circle_code=row["circle_code"],
        elder_name=row["elder_name"],
        timezone=row["timezone"],
        routine=_routine_from_json(row["routine_json"]),
        checkin_enabled=bool(row["checkin_enabled"]),
        checkin_hour=row["checkin_hour"],
        last_elder_activity_at=from_iso_or_none(row["last_elder_activity_at"]),
    )


def _member(row: sqlite3.Row) -> Member:
    return Member(
        id=row["id"],
        profile_id=row["profile_id"],
        name=row["name"],
        email=row["email"],
        role=Role(row["role"]),
        trusted_editor=bool(row["trusted_editor"]),
        receives_alerts=bool(row["receives_alerts"]),
        session_version=row["session_version"],
        active=bool(row["active"]),
    )
