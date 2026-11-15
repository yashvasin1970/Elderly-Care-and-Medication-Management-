"""Forecast when a medicine will run out."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from .models import Medication, Slot
from .schedule import doses_per_day

#: Warn this many days before the stock runs out.
REFILL_WARNING_DAYS = 5


@dataclass(frozen=True, slots=True)
class RefillForecast:
    """How long the current stock of one medicine will last."""

    medication_id: int
    name: str
    stock: int
    days_left: int
    runs_out_on: date
    needs_refill: bool


def forecast_refill(
    medication: Medication,
    slots: Iterable[Slot],
    today: date,
    warning_days: int = REFILL_WARNING_DAYS,
) -> RefillForecast | None:
    """Forecast the run-out date, or ``None`` when stock is not tracked."""
    if medication.stock_count is None:
        return None
    daily_units = doses_per_day(slots) * medication.units_per_dose
    if daily_units <= 0:
        return None
    days_left = int(max(medication.stock_count, 0) // daily_units)
    return RefillForecast(
        medication_id=medication.id,
        name=medication.name,
        stock=medication.stock_count,
        days_left=days_left,
        runs_out_on=today + timedelta(days=days_left),
        needs_refill=days_left <= warning_days,
    )
