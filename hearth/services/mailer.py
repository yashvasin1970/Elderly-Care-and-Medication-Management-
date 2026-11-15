"""Sending email, with a no-setup demo mode.

When SMTP settings are present, messages are sent for real. When they are
not, every message is still recorded and shown on the in-app Outbox page, so
the whole reminder flow can be seen without any credentials.
"""

from __future__ import annotations

import logging
import smtplib
import sqlite3
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Protocol

from ..repositories import records

log = logging.getLogger(__name__)


class Mailer(Protocol):
    """Anything that can send one plain-text email."""

    def send(self, recipient: str, subject: str, body: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SmtpMailer:
    """Send mail through an SMTP server, upgrading to TLS before signing in."""

    host: str
    port: int
    sender: str
    username: str = ""
    password: str = ""
    use_tls: bool = True
    timeout: float = 10.0

    def send(self, recipient: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = " ".join(subject.split())  # a subject is always one line
        message.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)


def mailer_from_config(config: Mapping[str, Any]) -> Mailer | None:
    """Build a mailer from settings, or ``None`` to run in outbox-only mode."""
    host = config.get("SMTP_HOST")
    sender = config.get("SMTP_FROM") or config.get("SMTP_USERNAME")
    if not host or not sender:
        return None
    return SmtpMailer(
        host=host,
        port=int(config.get("SMTP_PORT") or 587),
        sender=sender,
        username=config.get("SMTP_USERNAME") or "",
        password=config.get("SMTP_PASSWORD") or "",
        use_tls=bool(config.get("SMTP_USE_TLS", True)),
    )


def deliver(
    db: sqlite3.Connection,
    mailer: Mailer | None,
    *,
    profile_id: int,
    dedupe_key: str,
    recipient: str,
    kind: str,
    subject: str,
    body: str,
    now: datetime,
) -> bool:
    """Send a notification at most once.

    Returns True if this call created the notification, and False if it had
    already been handled by an earlier run.
    """
    notification_id = records.claim_notification(
        db,
        profile_id=profile_id,
        dedupe_key=dedupe_key,
        recipient=recipient,
        kind=kind,
        subject=subject,
        body=body,
        now=now,
    )
    if notification_id is None:
        return False
    if mailer is None:
        records.mark_notification(db, notification_id, "outbox")
        return True
    try:
        mailer.send(recipient, subject, body)
    except (OSError, smtplib.SMTPException, ValueError) as error:
        # A mail failure must never stop the rest of the reminder run.
        log.warning("Could not send %s notification: %s", kind, error)
        records.mark_notification(db, notification_id, "failed", str(error))
    else:
        records.mark_notification(db, notification_id, "sent")
    return True
