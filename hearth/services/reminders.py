"""The reminder job: work out what needs sending right now, and send it once.

The job is safe to run as often as you like (every minute locally, or from a
cloud scheduler). It only looks at the last two days of doses, and each
notification has a unique key, so a repeat run never emails anyone twice.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..access import Role
from ..engine.escalation import (
    Stage,
    appointment_stage,
    due_actions,
    needs_checkin,
    plan,
)
from ..engine.models import DoseLog, DoseStatus, Occurrence
from ..repositories import appointments, doses, medications, profiles
from ..repositories.profiles import Member, Profile
from ..timeutil import to_iso
from . import care, consent, messages
from .mailer import Mailer, deliver
from .messages import Message

DIGEST_WEEKDAY = 6  # Sunday
DIGEST_HOUR = 18
_APPOINTMENT_LOOKAHEAD = timedelta(hours=25)

_DOSE_KINDS = {
    Stage.UPCOMING: "upcoming",
    Stage.NUDGE: "nudge",
    Stage.CARETAKER: "missed",
    Stage.FAMILY: "missed",
}


@dataclass(frozen=True, slots=True)
class Audience:
    """Who can be emailed in a circle, grouped by role."""

    elder: tuple[Member, ...]
    caretakers: tuple[Member, ...]
    family: tuple[Member, ...]

    @classmethod
    def from_members(cls, members: list[Member]) -> Audience:
        reachable = [m for m in members if m.email and m.receives_alerts]
        return cls(
            elder=tuple(m for m in reachable if m.role is Role.ELDER),
            caretakers=tuple(m for m in reachable if m.role is Role.CARETAKER),
            family=tuple(m for m in reachable if m.role is Role.FAMILY),
        )

    @property
    def supporters(self) -> tuple[Member, ...]:
        return self.caretakers + self.family

    @property
    def everyone(self) -> tuple[Member, ...]:
        return self.elder + self.supporters

    def for_stage(self, stage: Stage) -> tuple[Member, ...]:
        if stage in (Stage.UPCOMING, Stage.NUDGE):
            return self.elder
        if stage is Stage.CARETAKER:
            # With no caretaker in the circle, family step in at this stage.
            return self.caretakers or self.family
        return self.family


@dataclass(slots=True)
class _Bucket:
    """Doses that share one reminder email."""

    occurrences: list[Occurrence] = field(default_factory=list)
    due: bool = False


def run(db: sqlite3.Connection, mailer: Mailer | None, now: datetime, base_url: str) -> int:
    """Send everything that is due for every profile. Returns how many were new."""
    return sum(
        run_profile(db, mailer, profile, now, base_url) for profile in profiles.list_profiles(db)
    )


def run_profile(
    db: sqlite3.Connection, mailer: Mailer | None, profile: Profile, now: datetime, base_url: str
) -> int:
    """Send everything that is due for one profile. Returns how many were new."""
    ctx = care.load_context(db, profile)
    audience = Audience.from_members(profiles.list_members(db, profile.id))
    links = _Links(base_url.rstrip("/"))

    def send(member: Member, key: str, kind: str, message: Message) -> int:
        return deliver(
            db,
            mailer,
            profile_id=profile.id,
            dedupe_key=f"p{profile.id}:{key}",
            recipient=member.email,
            kind=kind,
            subject=message.subject,
            body=message.body,
            now=now,
        )

    return (
        _dose_reminders(db, ctx, audience, now, links, send)
        + _appointment_reminders(db, ctx, audience, now, links, send)
        + _refill_reminders(ctx, audience, now, links, send)
        + _change_request_notices(db, ctx, audience, now, links, send)
        + _quiet_checkin(db, ctx, audience, now, links, send)
        + _weekly_digest(db, ctx, audience, now, links, send)
    )


_Send = Callable[[Member, str, str, Message], int]


@dataclass(frozen=True, slots=True)
class _Links:
    base: str

    @property
    def today(self) -> str:
        return f"{self.base}/today"

    @property
    def medicines(self) -> str:
        return f"{self.base}/medicines"

    @property
    def requests(self) -> str:
        return f"{self.base}/requests"

    @property
    def insights(self) -> str:
        return f"{self.base}/insights"

    def visit_summary(self, appointment_id: int) -> str:
        return f"{self.base}/appointments/{appointment_id}/summary"


# --- doses ----------------------------------------------------------------


def _dose_reminders(
    db: sqlite3.Connection,
    ctx: care.CareContext,
    audience: Audience,
    now: datetime,
    links: _Links,
    send: _Send,
) -> int:
    local_now = ctx.local(now)
    today = local_now.date()
    yesterday = today - timedelta(days=1)
    logs = doses.logs_between(db, ctx.profile.id, yesterday, today)

    # Doses due at the same moment share one email per stage.
    buckets: dict[tuple[Stage, str], _Bucket] = {}
    for day in (yesterday, today):
        for occ in ctx.occurrences(day):
            if not occ.medication.active:
                continue  # never remind anyone about a medicine that has been stopped
            log = logs.get((occ.slot.id, day))
            due_now = {action.stage for action in due_actions(occ, log, local_now)}
            for action in plan(occ, log):
                key = _bucket_key(action.stage, action.at, occ, log)
                bucket = buckets.setdefault((action.stage, key), _Bucket())
                bucket.occurrences.append(occ)
                bucket.due = bucket.due or action.stage in due_now

    elder = ctx.profile.elder_name
    sent = 0
    for (stage, key), bucket in buckets.items():
        if not bucket.due:
            continue
        occurrences = bucket.occurrences
        minutes_late = int((local_now - occurrences[0].scheduled_at).total_seconds() // 60)
        for member in audience.for_stage(stage):
            if stage is Stage.UPCOMING:
                message = messages.upcoming_doses(member.name, occurrences, links.today)
            elif stage is Stage.NUDGE:
                message = messages.nudge(member.name, occurrences, links.today)
            else:
                message = messages.missed_alert(
                    member.name, elder, occurrences, minutes_late, links.today
                )
            kind = _DOSE_KINDS[stage]
            sent += send(member, f"{kind}:{key}", kind, message)
    return sent


def _bucket_key(stage: Stage, at: datetime, occ: Occurrence, log: DoseLog | None) -> str:
    """Identify the email a dose reminder belongs to.

    A snoozed dose gets its own nudge when the snooze ends; everything else
    is grouped by the time the doses were due.
    """
    snoozed = log is not None and log.status is DoseStatus.SNOOZED
    if stage is Stage.NUDGE and snoozed:
        return f"{occ.dose_date}:slot{occ.slot.id}:snooze{at:%H%M}"
    return f"{occ.dose_date}:{occ.scheduled_at:%H%M}"


# --- appointments ---------------------------------------------------------


def _appointment_reminders(
    db: sqlite3.Connection,
    ctx: care.CareContext,
    audience: Audience,
    now: datetime,
    links: _Links,
    send: _Send,
) -> int:
    sent = 0
    upcoming = appointments.list_between(db, ctx.profile.id, now, now + _APPOINTMENT_LOOKAHEAD)
    for appointment in upcoming:
        stage = appointment_stage(appointment.starts_at, now)
        if stage is None:
            continue
        starts_local = ctx.local(appointment.starts_at)
        # The start time is part of the key, so a rescheduled visit reminds again.
        key = f"appointment:{appointment.id}:{stage}:{to_iso(appointment.starts_at)}"
        for member in audience.everyone:
            message = messages.appointment_reminder(
                member.name,
                ctx.profile.elder_name,
                appointment,
                starts_local,
                links.visit_summary(appointment.id),
            )
            sent += send(member, key, "appointment", message)
    return sent


# --- refills --------------------------------------------------------------


def _refill_reminders(
    ctx: care.CareContext, audience: Audience, now: datetime, links: _Links, send: _Send
) -> int:
    today = ctx.local(now).date()
    year, week, _ = today.isocalendar()
    sent = 0
    for forecast in care.refill_forecasts(ctx, today):
        if not forecast.needs_refill:
            continue
        # At most one reminder per medicine per week until it is restocked.
        key = f"refill:{forecast.medication_id}:{year}-W{week:02d}"
        for member in audience.everyone:
            message = messages.refill_reminder(
                member.name, ctx.profile.elder_name, forecast, links.medicines
            )
            sent += send(member, key, "refill", message)
    return sent


# --- suggested changes ----------------------------------------------------


def _change_request_notices(
    db: sqlite3.Connection,
    ctx: care.CareContext,
    audience: Audience,
    now: datetime,
    links: _Links,
    send: _Send,
) -> int:
    """Tell the elder when someone has suggested a change to their medicines."""
    if not audience.elder:
        return 0
    names = profiles.member_names(db, ctx.profile.id)
    sent = 0
    for request in medications.list_requests(db, ctx.profile.id, status="pending"):
        if request.created_at > now:
            continue  # not yet made at the moment this run represents

        proposer = names.get(request.proposed_by or 0, "Someone in your circle")
        summary = consent.describe_request(db, ctx.profile.id, request)
        for member in audience.elder:
            message = messages.change_requested(
                member.name, proposer, summary, request.reason, links.requests
            )
            sent += send(member, f"request:{request.id}", "request", message)
    return sent


# --- quiet check-in -------------------------------------------------------


def _quiet_checkin(
    db: sqlite3.Connection,
    ctx: care.CareContext,
    audience: Audience,
    now: datetime,
    links: _Links,
    send: _Send,
) -> int:
    local_now = ctx.local(now)
    profile = ctx.profile
    quiet = needs_checkin(
        local_now,
        enabled=profile.checkin_enabled,
        checkin_hour=profile.checkin_hour,
        last_activity=care.last_activity(db, profile),
    )
    # Only meaningful if something was actually expected of the elder today.
    expected = any(occ.scheduled_at <= local_now for occ in ctx.occurrences(local_now.date()))
    if not (quiet and expected):
        return 0
    key = f"checkin:{local_now.date()}"
    return sum(
        send(
            member,
            key,
            "checkin",
            messages.quiet_checkin(member.name, profile.elder_name, links.today),
        )
        for member in audience.supporters
    )


# --- weekly Care Weather --------------------------------------------------


def _weekly_digest(
    db: sqlite3.Connection,
    ctx: care.CareContext,
    audience: Audience,
    now: datetime,
    links: _Links,
    send: _Send,
) -> int:
    local_now = ctx.local(now)
    if local_now.weekday() != DIGEST_WEEKDAY or local_now.hour < DIGEST_HOUR:
        return 0
    year, week, _ = local_now.date().isocalendar()
    report = care.care_weather_report(db, ctx, now)
    key = f"digest:{year}-W{week:02d}"
    # The elder gets the same summary the family does: nothing is said behind their back.
    return sum(
        send(
            member,
            key,
            "digest",
            messages.weekly_digest(member.name, ctx.profile.elder_name, report, links.insights),
        )
        for member in audience.everyone
    )
