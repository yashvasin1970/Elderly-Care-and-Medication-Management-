"""Sessions, CSRF protection, permission checks and security headers."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from typing import Any, TypeVar

from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    g,
    redirect,
    request,
    session,
    url_for,
)

from ..access import Permission, can
from ..db import get_db
from ..repositories import profiles
from ..repositories.profiles import Member, Profile

_MEMBER = "member_id"
_PROFILE = "profile_id"
_VERSION = "session_version"
_CSRF = "csrf_token"
_STARTED = "started_at"  # when this sign-in began (seconds since the epoch)
_SEEN = "seen_at"  # when this session was last used
#: Only rewrite the "last used" time once a minute, not on every request.
_SEEN_RESOLUTION = 60

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Endpoints that authenticate with a bearer token instead of a session cookie.
_CSRF_EXEMPT = frozenset({"tasks.run_reminders"})

_CSP_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)
_CONTENT_SECURITY_POLICY = "; ".join(_CSP_DIRECTIVES)
#: Over HTTPS, also upgrade any stray http:// request instead of sending it in clear.
_CONTENT_SECURITY_POLICY_HTTPS = "; ".join((*_CSP_DIRECTIVES, "upgrade-insecure-requests"))
_ONE_YEAR = 365 * 24 * 60 * 60

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
    """Start a fresh session for a member (discarding any earlier one).

    Clearing first means a session identifier planted before sign-in can
    never become a signed-in session (session fixation).
    """
    stamp = int(now().timestamp())
    session.clear()
    session.permanent = True
    session[_MEMBER] = member.id
    session[_PROFILE] = member.profile_id
    session[_VERSION] = member.session_version
    session[_STARTED] = stamp
    session[_SEEN] = stamp


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
    if _session_expired():
        session.clear()
        flash("You were signed out to keep your circle safe. Please sign in again.", "info")
        return
    db = get_db()
    member = profiles.get_member(db, profile_id, member_id)
    profile = profiles.get_profile(db, profile_id)
    if member is None or profile is None or member.session_version != session.get(_VERSION):
        session.clear()
        return
    g.member = member
    g.profile = profile
    stamp = int(now().timestamp())
    if stamp - session.get(_SEEN, 0) >= _SEEN_RESOLUTION:
        session[_SEEN] = stamp


def _session_expired() -> bool:
    """Has this session been idle too long, or open too long altogether?

    The cookie's own expiry slides forward on every visit, so these checks are
    what really bound a session, even if its cookie were copied.
    """
    config = current_app.config
    stamp = now().timestamp()
    started, seen = session.get(_STARTED), session.get(_SEEN)
    if not isinstance(started, int) or not isinstance(seen, int):
        return True  # a session from before these checks existed
    too_long = stamp - started > config["PERMANENT_SESSION_LIFETIME"].total_seconds()
    idle = stamp - seen > config["SESSION_IDLE_TIMEOUT"].total_seconds()
    return too_long or idle


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
    https = current_app.config["SESSION_COOKIE_SECURE"]
    csp = _CONTENT_SECURITY_POLICY_HTTPS if https else _CONTENT_SECURITY_POLICY
    headers.setdefault("Content-Security-Policy", csp)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    # Keep other sites' windows and pages from reaching into Hearth's.
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    if https:
        # Browsers remember to use HTTPS only, even if someone types http://.
        headers.setdefault("Strict-Transport-Security", f"max-age={_ONE_YEAR}; includeSubDomains")
    if request.endpoint != "static":
        # Health information must not linger in shared or proxy caches.
        headers.setdefault("Cache-Control", "no-store")
    return response


def init_app(app: Flask) -> None:
    app.before_request(_load_member)
    app.before_request(_check_csrf)
    app.after_request(_add_security_headers)
