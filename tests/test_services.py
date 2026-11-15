"""Service-level rules: consent, schedule history and the optional Gemini helper."""

from __future__ import annotations

import json
import urllib.error
from datetime import timedelta

from hearth.access import Role, can, requires_elder_approval
from hearth.db import get_db
from hearth.repositories import medications, profiles
from hearth.repositories.medications import SlotInput
from hearth.services import consent, gemini, passcodes
from hearth.services.consent import ChangeOutcome

from .conftest import START, add_medicine, medicine

# --- roles ----------------------------------------------------------------


def test_only_the_elder_decides_and_manages_the_circle():
    from hearth.access import Permission

    for role in (Role.CARETAKER, Role.FAMILY, Role.VIEWER):
        assert not can(role, Permission.DECIDE_CHANGES)
        assert not can(role, Permission.MANAGE_CIRCLE)
    assert all(can(Role.ELDER, permission) for permission in Permission)


def test_approval_is_needed_unless_elder_or_trusted():
    assert not requires_elder_approval(Role.ELDER, trusted_editor=False)
    assert requires_elder_approval(Role.FAMILY, trusted_editor=False)
    assert not requires_elder_approval(Role.CARETAKER, trusted_editor=True)


# --- consent and history --------------------------------------------------


def _member(app, circle, role):
    with app.app_context():
        member = profiles.get_member(get_db(), circle.profile_id, circle.members[role])
    assert member is not None
    return member


def test_editing_a_schedule_keeps_unchanged_slots_and_retires_removed_ones(app, circle):
    two_doses = (SlotInput("breakfast", 0), SlotInput("dinner", 0))
    add_medicine(app, circle, medicine(slots=two_doses), when=START - timedelta(days=5))
    elder = _member(app, circle, Role.ELDER)
    with app.app_context():
        db = get_db()
        before = {s.anchor: s for s in medications.list_slots(db, circle.profile_id)}
        changed = medicine(slots=(SlotInput("breakfast", 0), SlotInput("bedtime", 0)))
        outcome = consent.submit_change(
            db, elder, action="update", medication_id=1, payload=changed, reason="", now=START
        )
        after = medications.list_slots(db, circle.profile_id)
    assert outcome is ChangeOutcome.APPLIED
    by_anchor = {(s.anchor, s.retired_at is None): s for s in after}
    assert by_anchor[("breakfast", True)].id == before["breakfast"].id  # history intact
    assert by_anchor[("dinner", False)].retired_at == START
    assert ("bedtime", True) in by_anchor


def test_a_request_can_only_be_decided_once(app, circle):
    family = _member(app, circle, Role.FAMILY)
    elder = _member(app, circle, Role.ELDER)
    with app.app_context():
        db = get_db()
        outcome = consent.submit_change(
            db,
            family,
            action="create",
            medication_id=None,
            payload=medicine(),
            reason="",
            now=START,
        )
        assert outcome is ChangeOutcome.PENDING
        assert medications.list_medications(db, circle.profile_id) == []
        assert consent.decide(db, elder, 1, approve=True, now=START)
        assert not consent.decide(db, elder, 1, approve=False, now=START)
        assert [m.name for m in medications.list_medications(db, circle.profile_id)] == [
            "Metformin"
        ]


def test_describe_changes_lists_only_what_differs():
    current = medicine()
    proposed = medicine(dose="850 mg", slots=(SlotInput("dinner", 15),))
    changes = {c.label: (c.before, c.after) for c in consent.describe_changes(current, proposed)}
    assert changes == {
        "Dose": ("500 mg", "850 mg"),
        "When": ("With breakfast", "15 min after dinner"),
    }


def test_describe_changes_for_a_new_medicine_lists_filled_fields():
    labels = [c.label for c in consent.describe_changes(None, medicine())]
    assert labels[:3] == ["Name", "Dose", "When"]
    assert "Ends" not in labels


def test_days_label():
    assert consent.days_label("1111111") == "Every day"
    assert consent.days_label("1010100") == "Mon, Wed, Fri"


# --- passcodes ------------------------------------------------------------


def test_passcode_rules():
    assert passcodes.passcode_problem("12345") is not None
    assert passcodes.passcode_problem("aaaaaaa") is not None
    assert passcodes.passcode_problem("x" * 65) is not None
    assert passcodes.passcode_problem("river-stone-7") is None


def test_generated_codes_are_readable():
    code = passcodes.generate_circle_code()
    assert len(code) == 8 and not set(code) & set("01OI")
    assert passcodes.normalise_circle_code(" abcd-efgh ") == "ABCDEFGH"
    assert passcodes.format_circle_code("ABCDEFGH") == "ABCD-EFGH"
    generated = passcodes.generate_passcode()
    assert passcodes.passcode_problem(generated) is None


# --- optional Gemini helper ----------------------------------------------


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def gemini_answering(answer: object):
    calls = []

    def opener(request, timeout):
        calls.append(request)
        return FakeResponse(
            {"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]}
        )

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


def test_gemini_answer_becomes_a_validated_draft():
    opener = gemini_answering(
        {
            "name": "Amlodipine",
            "dose": "5 mg",
            "slots": [{"anchor": "breakfast", "offset_minutes": 15}],
        }
    )
    draft = gemini.parse_with_gemini(
        "Amlodipine 5mg after breakfast", api_key="key-123", opener=opener
    )
    assert draft is not None
    assert draft.source == "gemini"
    assert draft.name == "Amlodipine"
    [request] = opener.calls
    assert request.get_header("X-goog-api-key") == "key-123"
    assert "key-123" not in request.full_url  # the key never goes in the URL
    body = json.loads(request.data)
    assert body["contents"][0]["parts"][0]["text"] == "Amlodipine 5mg after breakfast"
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_failures_fall_back_quietly():
    def broken(request, timeout):
        raise urllib.error.URLError("offline")

    assert gemini.parse_with_gemini("Aspirin 75 mg", api_key="k", opener=broken) is None
    assert (
        gemini.parse_with_gemini("Aspirin", api_key="k", opener=gemini_answering(["list"])) is None
    )
    assert (
        gemini.parse_with_gemini("Aspirin", api_key="k", opener=gemini_answering({"dose": "1"}))
        is None
    )


def test_invalid_model_name_is_never_requested():
    opener = gemini_answering({"name": "Aspirin", "slots": []})
    assert gemini.parse_with_gemini("Aspirin", api_key="k", model="../evil", opener=opener) is None
    assert opener.calls == []


def test_draft_uses_built_in_rules_without_a_key_or_when_gemini_fails(monkeypatch):
    without_key = gemini.draft_medication("Aspirin 75 mg at bedtime", {"GEMINI_API_KEY": None})
    assert without_key.source == "rules"

    monkeypatch.setattr(gemini, "parse_with_gemini", lambda *a, **k: None)
    failed = gemini.draft_medication("Aspirin 75 mg", {"GEMINI_API_KEY": "k"})
    assert failed.source == "rules" and failed.name == "Aspirin"


def test_draft_skips_gemini_when_rate_limited(monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("Gemini should not be called")

    monkeypatch.setattr(gemini, "parse_with_gemini", must_not_run)
    draft = gemini.draft_medication("Aspirin 75 mg", {"GEMINI_API_KEY": "k"}, use_ai=False)
    assert draft.source == "rules"
