"""Passcodes and circle codes: creating, hashing and checking them.

Passcodes are never stored or logged in plain text. Only a salted scrypt hash
is kept, so even someone with the database file cannot read them back.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from functools import lru_cache

from werkzeug.security import check_password_hash, generate_password_hash

from ..repositories import profiles

#: Eight characters is the floor NIST recommends for passwords chosen by people.
MIN_PASSCODE_LENGTH = 8
MAX_PASSCODE_LENGTH = 64
CIRCLE_CODE_LENGTH = 8
DEFAULT_HASH_METHOD = "scrypt"

# No 0/O or 1/I, which are easy to confuse when read aloud or copied by hand.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_NOT_ALPHANUMERIC = re.compile(r"[^A-Za-z0-9]")

#: Passcodes attackers try first. Compared after removing case and punctuation.
_COMMON_PASSCODES = frozenset(
    {
        "12345678", "123456789", "1234567890", "87654321", "11223344", "12341234",
        "password", "password1", "password123", "passw0rd", "passcode", "letmein",
        "welcome", "welcome1", "iloveyou", "sunshine", "princess", "qwerty", "qwertyuiop",
        "qwerty123", "asdfghjk", "zxcvbnm", "abc12345", "abcd1234", "admin123", "monkey123",
        "football", "baseball", "dragon123", "trustno1", "hearth", "hearth123", "family123",
        "mother", "father", "grandma", "grandpa", "medicine", "doctor123",
    }
)  # fmt: skip
_TOO_EASY = "That passcode is too easy to guess. Try a few words with a number, like river-stone-7."


def hash_passcode(passcode: str, method: str = DEFAULT_HASH_METHOD) -> str:
    return generate_password_hash(passcode, method=method)


def verify_passcode(passcode_hash: str, passcode: str) -> bool:
    return check_password_hash(passcode_hash, passcode)


def passcode_problem(passcode: str, *, avoid: tuple[str, ...] = ()) -> str | None:
    """Explain why a passcode is not acceptable, or return ``None`` if it is.

    ``avoid`` holds words the passcode must not contain, such as the person's
    own name, which is the first thing someone who knows them would try.
    """
    if len(passcode) < MIN_PASSCODE_LENGTH:
        return f"Use at least {MIN_PASSCODE_LENGTH} characters."
    if len(passcode) > MAX_PASSCODE_LENGTH:
        return f"Use at most {MAX_PASSCODE_LENGTH} characters."
    plain = _NOT_ALPHANUMERIC.sub("", passcode).lower()
    if plain in _COMMON_PASSCODES or _is_simple_pattern(plain):
        return _TOO_EASY
    for word in _words(avoid):
        if word in plain:
            return "Don't use a name in the passcode. It is the first thing someone would guess."
    return None


def _is_simple_pattern(plain: str) -> bool:
    """Spot passcodes like 11111111, abcdefgh, 98765432 or 12121212."""
    if len(set(plain)) <= 2:
        return True
    steps = {ord(b) - ord(a) for a, b in zip(plain, plain[1:], strict=False)}
    if len(steps) == 1 and abs(next(iter(steps))) <= 1:
        return True
    # The shortest repeating unit: "123123123" repeats every 3 characters.
    period = (plain + plain).find(plain, 1)
    return period <= 4 and len(plain) > period


def _words(texts: tuple[str, ...]) -> set[str]:
    """Words of three or more letters from names, lowercased."""
    found = set()
    for text in texts:
        found.update(word for word in re.findall(r"[a-z]+", text.lower()) if len(word) >= 3)
    return found


def generate_passcode() -> str:
    """A random eight-digit passcode, grouped so it is easy to read out."""
    while True:
        digits = "".join(secrets.choice("0123456789") for _ in range(8))
        passcode = f"{digits[:4]}-{digits[4:]}"
        if passcode_problem(passcode) is None:  # skip the rare 1234-5678
            return passcode


def generate_circle_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CIRCLE_CODE_LENGTH))


def normalise_circle_code(raw: str) -> str:
    """Strip spaces and dashes so "abcd-efgh" and "ABCDEFGH" are the same code."""
    return _NOT_ALPHANUMERIC.sub("", raw).upper()[:CIRCLE_CODE_LENGTH]


def format_circle_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) > 4 else code


def find_member_id(db: sqlite3.Connection, profile_id: int, passcode: str) -> int | None:
    """Return the member of a circle whose passcode matches, if any."""
    for member_id, passcode_hash in profiles.passcode_hashes(db, profile_id):
        if verify_passcode(passcode_hash, passcode):
            return member_id
    return None


def is_passcode_taken(db: sqlite3.Connection, profile_id: int, passcode: str) -> bool:
    """Passcodes identify a member, so each must be unique within a circle."""
    return find_member_id(db, profile_id, passcode) is not None


def burn_verification_time(passcode: str, method: str = DEFAULT_HASH_METHOD) -> None:
    """Do the same work as a real check when the circle code does not exist.

    Without this, a wrong circle code would answer faster than a wrong
    passcode, letting an attacker discover which circle codes are real.
    """
    verify_passcode(_decoy_hash(method), passcode)


@lru_cache(maxsize=4)
def _decoy_hash(method: str) -> str:
    return hash_passcode(secrets.token_urlsafe(16), method)
