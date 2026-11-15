"""The wording of every email Hearth sends.

Messages are short, plain text and written the way a considerate person
would say it. They never contain passcodes, and never give medical advice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ..engine.insights import CareWeather
from ..engine.models import Occurrence
from ..engine.refill import RefillForecast
from ..repositories.appointments import Appointment
from ..timeutil import friendly_clock as clock


@dataclass(frozen=True, slots=True)
class Message:
    subject: str
    body: str


def day_and_clock(moment: datetime) -> str:
    return f"{moment:%A} {moment.day} {moment:%B} at {clock(moment)}"


def upcoming_doses(name: str, occurrences: Sequence[Occurrence], link: str) -> Message:
    when = clock(occurrences[0].scheduled_at)
    return Message(
        f"Coming up at {when}: {_names(occurrences)}",
        _compose(
            f"Hello {name},",
            f"A gentle heads-up. At {when} it is time for:",
            _dose_lines(occurrences),
            f'When you have taken it, tap "Took it" here: {link}',
        ),
    )


def nudge(name: str, occurrences: Sequence[Occurrence], link: str) -> Message:
    when = clock(occurrences[0].scheduled_at)
    return Message(
        f"Still to confirm from {when}: {_names(occurrences)}",
        _compose(
            f"Hello {name},",
            f"These were due at {when} and have not been confirmed yet:",
            _dose_lines(occurrences),
            "If you have already taken them, just confirm it. If you would rather skip a dose,"
            f" you can record that too: {link}",
            _missed_rules(occurrences),
        ),
    )


def missed_alert(
    recipient: str, elder: str, occurrences: Sequence[Occurrence], minutes_late: int, link: str
) -> Message:
    when = clock(occurrences[0].scheduled_at)
    return Message(
        f"{elder} has not confirmed the {when} dose",
        _compose(
            f"Hello {recipient},",
            f"{elder} has not yet confirmed these doses, due at {when}"
            f" ({minutes_late} minutes ago):",
            _dose_lines(occurrences),
            f"{elder} was reminded first. A friendly call or check-in may help.",
            _missed_rules(occurrences),
            f"See today's doses: {link}",
        ),
    )


def appointment_reminder(
    recipient: str, elder: str, appointment: Appointment, starts_local: datetime, link: str
) -> Message:
    details = [f"What: {appointment.title}", f"When: {day_and_clock(starts_local)}"]
    if appointment.doctor:
        details.append(f"With: {appointment.doctor}")
    if appointment.location:
        details.append(f"Where: {appointment.location}")
    return Message(
        f"Appointment for {elder}: {appointment.title}, {day_and_clock(starts_local)}",
        _compose(
            f"Hello {recipient},",
            f"A reminder about an upcoming appointment for {elder}.",
            "\n".join(details),
            f"A one-page visit summary to take along is ready here: {link}",
        ),
    )


def refill_reminder(recipient: str, elder: str, forecast: RefillForecast, link: str) -> Message:
    days = "today" if forecast.days_left == 0 else f"in about {forecast.days_left} days"
    return Message(
        f"{forecast.name} is running low for {elder}",
        _compose(
            f"Hello {recipient},",
            f"{forecast.name} will run out {days} ({forecast.runs_out_on:%A %d %B})."
            f" There are {forecast.stock} left.",
            f"Once it has been refilled, update the stock here: {link}",
        ),
    )


def quiet_checkin(recipient: str, elder: str, link: str) -> Message:
    return Message(
        f"No sign of activity from {elder} yet today",
        _compose(
            f"Hello {recipient},",
            f"Nothing has been recorded for {elder} so far today. This is not an alarm,"
            " just a nudge: it might be a good moment to call and say hello.",
            f"{elder} can switch these check-ins off at any time in Settings.",
            f"See today's doses: {link}",
        ),
    )


def weekly_digest(recipient: str, elder: str, report: CareWeather, link: str) -> Message:
    insights = "\n\n".join(
        f"- {insight.message}\n  Idea: {insight.suggestion}" for insight in report.insights
    )
    return Message(
        f"Care Weather for {elder}: {report.weather.label}",
        _compose(
            f"Hello {recipient},",
            f"This week's Care Weather for {elder}: {report.weather.label}.",
            report.headline,
            insights,
            f"See the full picture: {link}",
        ),
    )


def change_requested(elder: str, proposer: str, summary: str, reason: str, link: str) -> Message:
    return Message(
        f"{proposer} suggested a change to your medicines",
        _compose(
            f"Hello {elder},",
            f"{proposer} has suggested: {summary}.",
            f"Their reason: {reason}" if reason else "",
            f"Nothing changes unless you approve it. Review it here: {link}",
        ),
    )


_FOOTER = (
    "--\nSent by Hearth because you are part of this care circle."
    " Hearth passes on reminders and never gives medical advice."
)


def _compose(*paragraphs: str) -> str:
    return "\n\n".join([p for p in paragraphs if p] + [_FOOTER])


def _names(occurrences: Sequence[Occurrence]) -> str:
    return ", ".join(occ.medication.name for occ in occurrences)


def _dose_lines(occurrences: Sequence[Occurrence]) -> str:
    lines = []
    for occ in occurrences:
        med = occ.medication
        line = f"- {med.name} {med.dose}".rstrip()
        if med.instructions:
            line += f" ({med.instructions})"
        lines.append(line)
    return "\n".join(lines)


def _missed_rules(occurrences: Sequence[Occurrence]) -> str:
    """Quote the label's own missed-dose instructions, where they were recorded."""
    rules = [
        f"- {occ.medication.name}: {occ.medication.missed_dose_rule}"
        for occ in occurrences
        if occ.medication.missed_dose_rule
    ]
    if not rules:
        return ""
    return "From the label or pharmacist, if a dose is late:\n" + "\n".join(rules)
