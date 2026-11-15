"""Learn the elder's real routine from when doses are actually taken.

If the "8:00 breakfast" pills are confirmed around 8:40 day after day, the
reminder is simply set at the wrong time. This module spots that and offers
a better time. It only ever suggests: the elder decides.

The method is deliberately simple and explainable: the median gap between
the scheduled and the actual time, over recent days, with a consistency check.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time
from statistics import median

#: Fewer confirmed doses than this is not enough to call it a habit.
MIN_SAMPLES = 5
#: Ignore shifts smaller than this many minutes; they are not worth a change.
MIN_SHIFT_MINUTES = 20
#: Gaps larger than this are treated as late logging, not as routine.
MAX_PLAUSIBLE_GAP_MINUTES = 180
#: Share of observations that must lean the same way as the median.
CONSISTENCY = 0.7
#: Suggested times are rounded to this many minutes.
ROUND_TO_MINUTES = 5

_LAST_MINUTE_OF_DAY = 23 * 60 + 59


@dataclass(frozen=True, slots=True)
class Observation:
    """One confirmed dose: which routine group it belongs to, and the timing."""

    group: str
    scheduled_at: datetime
    taken_at: datetime


@dataclass(frozen=True, slots=True)
class RhythmSuggestion:
    """A proposal to move a routine time to match what really happens."""

    group: str
    current: time
    suggested: time
    shift_minutes: int
    samples: int

    @property
    def key(self) -> str:
        """Stable identifier, used to remember a dismissed suggestion."""
        return f"rhythm:{self.group}:{self.suggested:%H%M}"


def suggest_shifts(
    observations: Iterable[Observation],
    current_times: Mapping[str, time],
) -> list[RhythmSuggestion]:
    """Suggest new times for routine groups whose doses run consistently off.

    ``current_times`` maps each group (an anchor such as "breakfast", or a
    single fixed slot) to its present clock time.
    """
    gaps: dict[str, list[float]] = defaultdict(list)
    for obs in observations:
        minutes = (obs.taken_at - obs.scheduled_at).total_seconds() / 60
        if abs(minutes) <= MAX_PLAUSIBLE_GAP_MINUTES:
            gaps[obs.group].append(minutes)

    suggestions: list[RhythmSuggestion] = []
    for group in sorted(gaps):
        current = current_times.get(group)
        values = gaps[group]
        if current is None or len(values) < MIN_SAMPLES:
            continue
        shift = median(values)
        if abs(shift) < MIN_SHIFT_MINUTES or not _is_consistent(values, shift):
            continue
        rounded = round(shift / ROUND_TO_MINUTES) * ROUND_TO_MINUTES
        suggested = shift_time(current, rounded)
        if suggested != current:
            suggestions.append(RhythmSuggestion(group, current, suggested, rounded, len(values)))
    return suggestions


def shift_time(value: time, minutes: int) -> time:
    """Move a clock time by ``minutes``, staying within the same day."""
    total = value.hour * 60 + value.minute + minutes
    total = max(0, min(total, _LAST_MINUTE_OF_DAY))
    return time(total // 60, total % 60)


def _is_consistent(values: list[float], shift: float) -> bool:
    """True when most observations lean clearly the same way as the median."""
    threshold = MIN_SHIFT_MINUTES / 2
    agreeing = sum(1 for v in values if v * shift > 0 and abs(v) >= threshold)
    return agreeing / len(values) >= CONSISTENCY
