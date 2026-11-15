"""Append-only records: the journal, the change history and the email outbox."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..timeutil import from_iso, to_iso

JOURNAL_KINDS: tuple[str, ...] = ("symptom", "question", "note")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    id: int
    member_id: int | None
    kind: str
    body: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    member_id: int | None
    action: str
    detail: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Notification:
    id: int
    recipient: str
    kind: str
    subject: str
    body: str
    status: str
    error: str
    created_at: datetime


# --- journal --------------------------------------------------------------


def add_journal_entry(
    db: sqlite3.Connection,
    *,
    profile_id: int,
    member_id: int,
    kind: str,
    body: str,
    now: datetime,
) -> None:
    db.execute(
        "INSERT INTO journal_entries (profile_id, member_id, kind, body, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (profile_id, member_id, kind, body, to_iso(now)),
    )


def list_journal(
    db: sqlite3.Connection,
    profile_id: int,
    *,
    since: datetime | None = None,
    limit: int = 100,
) -> list[JournalEntry]:
    """Journal entries, newest first, optionally only those after ``since``."""
    sql = "SELECT * FROM journal_entries WHERE profile_id = ?"
    params: list[object] = [profile_id]
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(to_iso(since))
    rows = db.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", (*params, limit))
    return [
        JournalEntry(
            row["id"], row["member_id"], row["kind"], row["body"], from_iso(row["created_at"])
        )
        for row in rows
    ]


# --- change history -------------------------------------------------------


def add_audit(
    db: sqlite3.Connection,
    *,
    profile_id: int,
    member_id: int | None,
    action: str,
    detail: str = "",
    now: datetime,
) -> None:
    db.execute(
        "INSERT INTO audit_log (profile_id, member_id, action, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (profile_id, member_id, action, detail, to_iso(now)),
    )


def list_audit(db: sqlite3.Connection, profile_id: int, limit: int = 200) -> list[AuditEntry]:
    rows = db.execute(
        "SELECT * FROM audit_log WHERE profile_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (profile_id, limit),
    )
    return [
        AuditEntry(
            row["id"], row["member_id"], row["action"], row["detail"], from_iso(row["created_at"])
        )
        for row in rows
    ]


# --- email outbox ---------------------------------------------------------


def claim_notification(
    db: sqlite3.Connection,
    *,
    profile_id: int,
    dedupe_key: str,
    recipient: str,
    kind: str,
    subject: str,
    body: str,
    now: datetime,
) -> int | None:
    """Reserve a notification for sending.

    Returns the new row id, or ``None`` if this exact notification was already
    claimed, which is what stops the same reminder being emailed twice.
    """
    cursor = db.execute(
        "INSERT OR IGNORE INTO notifications (profile_id, dedupe_key, recipient, kind, subject,"
        " body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (profile_id, dedupe_key, recipient, kind, subject, body, to_iso(now)),
    )
    return int(cursor.lastrowid) if cursor.rowcount == 1 and cursor.lastrowid else None


def mark_notification(
    db: sqlite3.Connection, notification_id: int, status: str, error: str = ""
) -> None:
    db.execute(
        "UPDATE notifications SET status = ?, error = ? WHERE id = ?",
        (status, error[:300], notification_id),
    )


def list_notifications(
    db: sqlite3.Connection, profile_id: int, limit: int = 100
) -> list[Notification]:
    rows = db.execute(
        "SELECT * FROM notifications WHERE profile_id = ?"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (profile_id, limit),
    )
    return [
        Notification(
            id=row["id"],
            recipient=row["recipient"],
            kind=row["kind"],
            subject=row["subject"],
            body=row["body"],
            status=row["status"],
            error=row["error"],
            created_at=from_iso(row["created_at"]),
        )
        for row in rows
    ]
