"""Sessions, CSRF protection, permission checks and security headers."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from typing import Any, TypeVar

from flask import Flask, Response, abort, current_app, g, redirect, request, session, url_for

from ..access import Permission, can
from ..db import get_db
from ..repositories import profiles
from ..repositories.profiles import Member, Profile

_MEMBER = "member_id"
_PROFILE = "profile_id"
_VERSION = "session_version"
_CSRF = "csrf_token"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Endpoints that authenticate with a bearer token instead of a session cookie.
_CSRF_EXEMPT = frozenset({"tasks.run_reminders"})

_CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

View = TypeVar("View", bound=Callable[..., Any])


def now() -> datetime:
    """The current moment, from the app's (replaceable) clock."""
    return current_app.config["CLOCK"]()


# --- CSRF -----------------------------------------------------------------


def csrf_token() -> str:
    """Return this session's CSRF token, creating it on first use."""
    token = session.get(_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_CSRF] = token
    return token


def _check_csrf() -> None:
    if request.method in _SAFE_METHODS or request.endpoint in _CSRF_EXEMPT:
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get(_CSRF, "")
    if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
        abort(
            400, description="Your session expired or the form was out of date. Please try again."
        )


# --- sessions -------------------------------------------------------------


def sign_in(member: Member) -> None:
    """Start a fresh session for a member (discarding any earlier one)."""
    session.clear()
    session.permanent = True
    session[_MEMBER] = member.id
    session[_PROFILE] = member.profile_id
    session[_VERSION] = member.session_version


def sign_out() -> None:
    session.clear()


def _load_member() -> None:
    """Attach the signed-in member and profile to the request, if any.

    The member is re-read on every request, so removing someone from the
    circle or changing their passcode ends their sessions immediately.
    """
    g.member = None
    g.profile = None
    if request.endpoint == "static":
        return  # stylesheets and scripts need no database lookup
    member_id, profile_id = session.get(_MEMBER), session.get(_PROFILE)
    if member_id is None or profile_id is None:
        return
    db = get_db()
    member = profiles.get_member(db, profile_id, member_id)
    profile = profiles.get_profile(db, profile_id)
    if member is None or profile is None or member.session_version != session.get(_VERSION):
        session.clear()
        return
    g.member = member
    g.profile = profile


def current_member() -> Member:
    member: Member | None = g.get("member")
    if member is None:
        abort(401)
    return member


def current_profile() -> Profile:
    profile: Profile | None = g.get("profile")
    if profile is None:
        abort(401)
    return profile


def allowed(permission: Permission) -> bool:
    """Does the signed-in member hold this permission?"""
    member: Member | None = g.get("member")
    return member is not None and can(member.role, permission)


def require(permission: Permission) -> Callable[[View], View]:
    """Guard a view: the visitor must be signed in and hold ``permission``."""

    def decorator(view: View) -> View:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if g.get("member") is None:
                return redirect(url_for("auth.home"))
            if not allowed(permission):
                abort(403)
            return view(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    return decorator


# --- headers --------------------------------------------------------------


def _add_security_headers(response: Response) -> Response:
    headers = response.headers
    headers.setdefault("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.endpoint != "static":
        # Health information must not linger in shared or proxy caches.
        headers.setdefault("Cache-Control", "no-store")
    return response


def init_app(app: Flask) -> None:
    app.before_request(_load_member)
    app.before_request(_check_csrf)
    app.after_request(_add_security_headers)
