"""Turn dose history into "Care Weather": a short, human summary for the circle.

Family members do not need a stream of percentages. They need to know whether
the week went well and, if not, what would actually help. Every pattern here
is a plain counting rule, so each insight can be explained and tested.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .models import DoseStatus
from .schedule import part_of_day

#: A pattern is only reported when at least this many doses were missed.
MIN_PATTERN_MISSES = 3
#: A medicine skipped this often by choice is worth raising with the doctor.
MIN_SKIPS_TO_FLAG = 3

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True, slots=True)
class DoseRecord:
    """The final outcome of one past dose (taken, skipped or missed)."""

    medication_id: int
    medication_name: str
    scheduled_at: datetime
    status: DoseStatus


@dataclass(frozen=True, slots=True)
class Adherence:
    """Counts of how past doses turned out."""

    taken: int = 0
    skipped: int = 0
    missed: int = 0

    @property
    def total(self) -> int:
        return self.taken + self.skipped + self.missed

    @property
    def rate(self) -> int | None:
        """Percentage of doses taken, or ``None`` when nothing was scheduled."""
        if self.total == 0:
            return None
        return round(100 * self.taken / self.total)


@dataclass(frozen=True, slots=True)
class Weather:
    """A weather-style reading of adherence."""

    code: str
    symbol: str
    label: str


@dataclass(frozen=True, slots=True)
class Insight:
    """A pattern worth knowing about, with one practical suggestion."""

    kind: str
    message: str
    suggestion: str


@dataclass(frozen=True, slots=True)
class CareWeather:
    """The full weekly picture shown to the circle."""

    weather: Weather
    adherence: Adherence
    headline: str
    insights: tuple[Insight, ...]


def summarise(records: Sequence[DoseRecord]) -> Adherence:
    """Count taken, skipped and missed doses."""
    counts = Counter(record.status for record in records)
    return Adherence(
        taken=counts[DoseStatus.TAKEN],
        skipped=counts[DoseStatus.SKIPPED],
        missed=counts[DoseStatus.MISSED],
    )


def by_medication(records: Sequence[DoseRecord]) -> dict[str, Adherence]:
    """Adherence for each medicine, in alphabetical order."""
    names = sorted({record.medication_name for record in records}, key=str.lower)
    return {name: summarise([r for r in records if r.medication_name == name]) for name in names}


def by_day(records: Sequence[DoseRecord]) -> dict[date, Adherence]:
    """Adherence for each day that had doses, oldest first."""
    days = sorted({record.scheduled_at.date() for record in records})
    return {day: summarise([r for r in records if r.scheduled_at.date() == day]) for day in days}


@dataclass(frozen=True, slots=True)
class DayBar:
    """One day of the 7-day chart."""

    day: date
    adherence: Adherence
    band: str
    height: int  # percentage of the chart height, in steps of 5


def week_bars(records: Sequence[DoseRecord], today: date, days: int = 7) -> list[DayBar]:
    """One bar per day for the last ``days`` days, including days with no doses."""
    bars = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        adherence = summarise([r for r in records if r.scheduled_at.date() == day])
        rate = adherence.rate
        height = 0 if rate is None else round(rate / 5) * 5
        bars.append(DayBar(day, adherence, band_for(rate), height))
    return bars


def band_for(rate: int | None) -> str:
    """Group a rate for charts: "good" (90%+), "fair" (75-89%), "low" or "none"."""
    if rate is None:
        return "none"
    if rate >= 90:
        return "good"
    return "fair" if rate >= 75 else "low"


def weather_for(adherence: Adherence) -> Weather:
    """Map an adherence rate to a weather reading."""
    rate = adherence.rate
    if rate is None:
        return Weather("clear", "—", "Nothing scheduled")
    if rate >= 90:
        return Weather("sunny", "☀️", "Sunny")
    if rate >= 75:
        return Weather("fair", "⛅", "Mostly sunny")
    if rate >= 50:
        return Weather("cloudy", "☁️", "Cloudy")
    return Weather("stormy", "⛈️", "Stormy")


def find_patterns(
    records: Sequence[DoseRecord],
    appointment_days: Collection[date] = (),
) -> list[Insight]:
    """Look for patterns in missed and skipped doses."""
    missed = [r for r in records if r.status is DoseStatus.MISSED]
    candidates: list[Insight | None] = []
    if len(missed) >= MIN_PATTERN_MISSES:
        candidates += [
            _time_of_day_pattern(missed),
            _weekday_pattern(missed),
            _medication_pattern(missed),
        ]
    candidates.append(_appointment_pattern(missed, appointment_days))
    candidates += _skip_patterns(records)
    if not missed and summarise(records).taken >= 7:
        candidates.append(
            Insight("steady", "Every scheduled dose was confirmed.", "Nothing needs to change.")
        )
    return [insight for insight in candidates if insight is not None]


def care_weather(
    week: Sequence[DoseRecord],
    history: Sequence[DoseRecord],
    appointment_days: Collection[date],
    name: str,
) -> CareWeather:
    """Build the Care Weather report.

    ``week`` drives the headline; the longer ``history`` gives the pattern
    rules enough data to say something reliable.
    """
    adherence = summarise(week)
    return CareWeather(
        weather=weather_for(adherence),
        adherence=adherence,
        headline=_headline(adherence, name),
        insights=tuple(find_patterns(history, appointment_days)),
    )


def _headline(adherence: Adherence, name: str) -> str:
    if adherence.rate is None:
        return f"No doses were scheduled for {name} this week."
    parts = [f"{name} took {adherence.taken} of {adherence.total} doses ({adherence.rate}%)"]
    if adherence.missed:
        parts.append(f"{adherence.missed} missed")
    if adherence.skipped:
        parts.append(f"{adherence.skipped} skipped by choice")
    return ", ".join(parts) + "."


def _time_of_day_pattern(missed: Sequence[DoseRecord]) -> Insight | None:
    part, count = Counter(part_of_day(r.scheduled_at) for r in missed).most_common(1)[0]
    if count / len(missed) < 0.6:
        return None
    return Insight(
        "time_of_day",
        f"Most missed doses are in the {part} ({count} of {len(missed)}).",
        f"A call or a check-in in the {part} could help.",
    )


def _weekday_pattern(missed: Sequence[DoseRecord]) -> Insight | None:
    weekday, count = Counter(r.scheduled_at.weekday() for r in missed).most_common(1)[0]
    if count < 2 or count / len(missed) < 0.4:
        return None
    day = _WEEKDAYS[weekday]
    return Insight(
        "weekday",
        f"{day}s are the hardest day ({count} of {len(missed)} missed doses).",
        f"Is something different about {day}s? An extra reminder that day may help.",
    )


def _medication_pattern(missed: Sequence[DoseRecord]) -> Insight | None:
    names = Counter(r.medication_name for r in missed)
    if len(names) < 2:
        return None  # with a single medicine this would state the obvious
    name, count = names.most_common(1)[0]
    if count / len(missed) < 0.5:
        return None
    return Insight(
        "medication",
        f"{name} is missed most often ({count} of {len(missed)} missed doses).",
        "Ask the doctor or pharmacist whether it could move to an easier time of day.",
    )


def _appointment_pattern(
    missed: Sequence[DoseRecord], appointment_days: Collection[date]
) -> Insight | None:
    if not appointment_days:
        return None
    days = {r.scheduled_at.date() for r in missed} & set(appointment_days)
    count = sum(1 for r in missed if r.scheduled_at.date() in days)
    if count < 2:
        return None
    return Insight(
        "appointment_day",
        f"{count} doses were missed on days with a doctor visit.",
        "On visit days, pack the doses to take along or set an extra reminder.",
    )


def _skip_patterns(records: Sequence[DoseRecord]) -> list[Insight]:
    skips = Counter(r.medication_name for r in records if r.status is DoseStatus.SKIPPED)
    return [
        Insight(
            "skipped",
            f"{name} was skipped by choice {count} times.",
            "Worth raising at the next doctor visit, in case it is causing side effects.",
        )
        for name, count in sorted(skips.items())
        if count >= MIN_SKIPS_TO_FLAG
    ]
