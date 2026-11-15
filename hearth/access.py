"""Who may do what in a care circle.

The elder owns the circle. Everyone else holds a role the elder gave them,
and every permission check in the app goes through ``can``.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ELDER = "elder"
    CARETAKER = "caretaker"
    FAMILY = "family"
    VIEWER = "viewer"


class Permission(StrEnum):
    VIEW_CARE = "view_care"
    LOG_DOSES = "log_doses"
    PROPOSE_MEDICATION_CHANGES = "propose_medication_changes"
    DECIDE_CHANGES = "decide_changes"
    MANAGE_APPOINTMENTS = "manage_appointments"
    WRITE_JOURNAL = "write_journal"
    VIEW_ACTIVITY = "view_activity"
    MANAGE_ROUTINE = "manage_routine"
    MANAGE_CIRCLE = "manage_circle"
    MANAGE_SETTINGS = "manage_settings"
    ASK_FOR_HELP = "ask_for_help"


_P = Permission

_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ELDER: frozenset(Permission),
    Role.CARETAKER: frozenset(
        {
            _P.VIEW_CARE,
            _P.LOG_DOSES,
            _P.PROPOSE_MEDICATION_CHANGES,
            _P.MANAGE_APPOINTMENTS,
            _P.WRITE_JOURNAL,
            _P.VIEW_ACTIVITY,
        }
    ),
    Role.FAMILY: frozenset(
        {
            _P.VIEW_CARE,
            _P.PROPOSE_MEDICATION_CHANGES,
            _P.MANAGE_APPOINTMENTS,
            _P.WRITE_JOURNAL,
            _P.VIEW_ACTIVITY,
        }
    ),
    Role.VIEWER: frozenset({_P.VIEW_CARE}),
}

ROLE_LABELS: dict[Role, str] = {
    Role.ELDER: "Circle owner",
    Role.CARETAKER: "Caretaker",
    Role.FAMILY: "Family",
    Role.VIEWER: "Viewer",
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.ELDER: "Owns the circle and has the final say on every change.",
    Role.CARETAKER: "Records doses, suggests medicine changes and manages appointments.",
    Role.FAMILY: "Sees how things are going, gets alerts and can suggest changes.",
    Role.VIEWER: "Can look, but cannot change anything. Good for a doctor.",
}

#: Roles the elder can hand out. There is exactly one elder per circle.
ASSIGNABLE_ROLES: tuple[Role, ...] = (Role.CARETAKER, Role.FAMILY, Role.VIEWER)


def can(role: Role, permission: Permission) -> bool:
    """Return whether a role holds a permission."""
    return permission in _ROLE_PERMISSIONS[role]


def requires_elder_approval(role: Role, trusted_editor: bool) -> bool:
    """Does a medicine change by this member need the elder's approval?

    The elder's own changes apply at once. So do changes by someone the elder
    has chosen to trust: delegating is also the elder's decision to make.
    """
    return role is not Role.ELDER and not trusted_editor
