"""Changes to the elder's medicines, with the elder in charge.

A change made by the elder, or by someone the elder has chosen to trust,
applies straight away. A change from anyone else becomes a request that waits
for the elder to approve or decline. Either way it is written to the history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ..access import requires_elder_approval
from ..db import transaction
from ..engine.models import FIXED, Slot
from ..engine.schedule import describe_slot
from ..repositories import medications, records
from ..repositories.medications import ChangeRequest, MedicationInput, SlotInput
from ..repositories.profiles import Member

ACTIONS: tuple[str, ...] = ("create", "update", "archive")

_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_CRITICALITY_LABELS = {
    "critical": "Critical: alert others quickly",
    "important": "Important: alert a caretaker if late",
    "routine": "Routine: weekly summary only",
}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ChangeOutcome(StrEnum):
    APPLIED = "applied"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One line of a before/after comparison shown to the elder."""

    label: str
    before: str
    after: str


def submit_change(
    db: sqlite3.Connection,
    member: Member,
    *,
    action: str,
    medication_id: int | None,
    payload: MedicationInput | None,
    reason: str,
    now: datetime,
) -> ChangeOutcome:
    """Apply a medicine change, or file it for the elder's approval."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown change action: {action}")
    summary = _summary(db, member.profile_id, action, medication_id, payload)
    with transaction(db):
        if requires_elder_approval(member.role, member.trusted_editor):
            medications.create_request(
                db,
                profile_id=member.profile_id,
                medication_id=medication_id,
                action=action,
                payload=payload,
                reason=reason,
                proposed_by=member.id,
                now=now,
            )
            _audit(db, member, "Suggested a change", summary, now)
            return ChangeOutcome.PENDING
        _apply(db, member.profile_id, action, medication_id, payload, now)
        _audit(db, member, _DIRECT_ACTIONS[action], summary, now)
        return ChangeOutcome.APPLIED


def decide(
    db: sqlite3.Connection, elder: Member, request_id: int, *, approve: bool, now: datetime
) -> bool:
    """Record the elder's decision on a request, applying it if approved.

    Returns False when the request does not exist or was already decided.
    """
    request = medications.get_request(db, elder.profile_id, request_id)
    if request is None or request.status != "pending":
        return False
    summary = _summary(db, elder.profile_id, request.action, request.medication_id, request.payload)
    with transaction(db):
        decided = medications.decide_request(
            db,
            elder.profile_id,
            request_id,
            status="approved" if approve else "declined",
            decided_by=elder.id,
            now=now,
        )
        if not decided:
            return False
        if approve:
            _apply(
                db, elder.profile_id, request.action, request.medication_id, request.payload, now
            )
        action = "Approved a suggested change" if approve else "Declined a suggested change"
        _audit(db, elder, action, summary, now)
    return True


def describe_request(db: sqlite3.Connection, profile_id: int, request: ChangeRequest) -> str:
    """A one-line description of what a request asks for."""
    return _summary(db, profile_id, request.action, request.medication_id, request.payload)


def describe_changes(
    current: MedicationInput | None, proposed: MedicationInput | None
) -> list[FieldChange]:
    """List what a proposal would change, in plain words.

    With no ``current`` (a new medicine) every filled-in field is listed.
    """
    if proposed is None:
        return []
    after = _display_fields(proposed)
    before = _display_fields(current) if current else dict.fromkeys(after, "")
    return [
        FieldChange(label, before[label], value)
        for label, value in after.items()
        if value != before[label]
    ]


def slot_label(slot: SlotInput) -> str:
    """Describe a form slot in everyday words."""
    if slot.anchor == FIXED:
        return f"At {slot.fixed_time}"
    return describe_slot(Slot(0, 0, slot.anchor, _EPOCH, slot.offset_minutes))


def days_label(days_mask: str) -> str:
    if days_mask == "1111111":
        return "Every day"
    return ", ".join(name for name, flag in zip(_DAY_NAMES, days_mask, strict=True) if flag == "1")


_DIRECT_ACTIONS = {
    "create": "Added a medicine",
    "update": "Changed a medicine",
    "archive": "Stopped a medicine",
}


def _apply(
    db: sqlite3.Connection,
    profile_id: int,
    action: str,
    medication_id: int | None,
    payload: MedicationInput | None,
    now: datetime,
) -> None:
    if action == "create" and payload is not None:
        medications.create_medication(db, profile_id, payload, now)
    elif action == "update" and payload is not None and medication_id is not None:
        medications.update_medication(db, profile_id, medication_id, payload, now)
    elif action == "archive" and medication_id is not None:
        medications.archive_medication(db, profile_id, medication_id, now)
    else:
        raise ValueError("The change is missing the details needed to apply it.")


def _summary(
    db: sqlite3.Connection,
    profile_id: int,
    action: str,
    medication_id: int | None,
    payload: MedicationInput | None,
) -> str:
    if action == "create" and payload is not None:
        return f"Add {payload.name} {payload.dose}".strip()
    current = medications.get_medication(db, profile_id, medication_id) if medication_id else None
    name = current.name if current else "a medicine"
    return f"Stop {name}" if action == "archive" else f"Change {name}"


def _audit(db: sqlite3.Connection, member: Member, action: str, detail: str, now: datetime) -> None:
    records.add_audit(
        db, profile_id=member.profile_id, member_id=member.id, action=action, detail=detail, now=now
    )


def _display_fields(data: MedicationInput) -> dict[str, str]:
    return {
        "Name": data.name,
        "Dose": data.dose,
        "When": "; ".join(slot_label(slot) for slot in data.slots),
        "Days": days_label(data.days_mask),
        "How important": _CRITICALITY_LABELS.get(data.criticality, data.criticality),
        "Instructions": data.instructions,
        "If a dose is missed": data.missed_dose_rule,
        "Looks like": f"{data.pill_color} {data.pill_shape}",
        "Amount per dose": str(data.units_per_dose),
        "In stock": "" if data.stock_count is None else str(data.stock_count),
        "Starts": data.start_date,
        "Ends": data.end_date or "",
    }
