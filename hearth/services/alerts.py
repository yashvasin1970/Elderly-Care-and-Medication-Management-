"""Alerts sent on demand, rather than by the scheduled reminder job.

* ``ask_for_help``: the elder presses "I need help" and everyone who supports
  them is emailed straight away.
* ``send_test_alert``: the demo's test buttons send a real reminder email
  (clearly marked "[Test]") so the alert flow can be seen without waiting.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from ..engine.escalation import POLICIES
from ..repositories import profiles, records
from ..repositories.profiles import Member, Profile
from . import care, messages
from .mailer import Mailer, deliver
from .reminders import Audience

TEST_KINDS: tuple[str, ...] = ("missed", "upcoming")
#: How long a help request stays highlighted on the circle's dashboards.
HELP_SHOWN_FOR = timedelta(hours=3)


def ask_for_help(
    db: sqlite3.Connection,
    mailer: Mailer | None,
    profile: Profile,
    elder: Member,
    now: datetime,
    base_url: str,
) -> list[str]:
    """Email every supporter that the elder asked for help.

    Returns the names of the people who were told. Pressing the button twice
    in the same minute sends one message, not two.
    """
    audience = Audience.from_members(profiles.list_members(db, profile.id))
    asked_at = care.load_context(db, profile).local(now)
    told: list[str] = []
    for member in audience.supporters:
        deliver(
            db,
            mailer,
            profile_id=profile.id,
            dedupe_key=f"p{profile.id}:help:{asked_at:%Y%m%d%H%M}",
            recipient=member.email,
            kind="help",
            **_fields(
                messages.help_request(
                    member.name, profile.elder_name, asked_at, f"{base_url.rstrip('/')}/today"
                )
            ),
            now=now,
        )
        told.append(member.name)
    records.add_audit(
        db,
        profile_id=profile.id,
        member_id=elder.id,
        action="Asked for help",
        detail=", ".join(told) if told else "No one with an email address to tell",
        now=now,
    )
    return told


def recent_help_request(db: sqlite3.Connection, profile_id: int, now: datetime) -> datetime | None:
    """When the elder last asked for help, if it was within the last few hours."""
    return records.latest_notification_time(db, profile_id, "help", since=now - HELP_SHOWN_FOR)


def send_test_alert(
    db: sqlite3.Connection,
    mailer: Mailer | None,
    profile: Profile,
    kind: str,
    now: datetime,
    base_url: str,
) -> int:
    """Send a marked test version of a real reminder. Returns how many were sent.

    "missed" sends the alert a caretaker gets when a dose is not confirmed;
    "upcoming" sends the heads-up the elder gets before a dose. Both use the
    next dose due today (or the last one, late in the evening).
    """
    if kind not in TEST_KINDS:
        raise ValueError(f"Unknown test alert: {kind}")
    ctx = care.load_context(db, profile)
    local_now = ctx.local(now)
    today = [occ for occ in ctx.occurrences(local_now.date()) if occ.medication.active]
    if not today:
        return 0
    target = next((occ for occ in today if occ.scheduled_at >= local_now), today[-1])
    group = [occ for occ in today if occ.scheduled_at == target.scheduled_at]
    audience = Audience.from_members(profiles.list_members(db, profile.id))
    link = f"{base_url.rstrip('/')}/today"
    policy = POLICIES[target.medication.criticality]
    minutes_late = int((policy.caretaker_after or policy.nudge_after).total_seconds() // 60)

    def compose(member: Member) -> messages.Message:
        if kind == "missed":
            return messages.missed_alert(member.name, profile.elder_name, group, minutes_late, link)
        return messages.upcoming_doses(member.name, group, link)

    recipients = (audience.caretakers or audience.family) if kind == "missed" else audience.elder
    sent = 0
    for member in recipients:
        sent += deliver(
            db,
            mailer,
            profile_id=profile.id,
            dedupe_key=f"p{profile.id}:test:{kind}:{now.isoformat()}",
            recipient=member.email,
            kind="test",
            **_fields(messages.as_test(compose(member))),
            now=now,
        )
    return sent


def _fields(message: messages.Message) -> dict[str, str]:
    return {"subject": message.subject, "body": message.body}
