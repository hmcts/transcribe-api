"""BenchNotes roles and permissions (DIAAT-373, DIAAT-376): the one role -> permission map.

Roles
-----
``Judge``, ``LegalTextManager``, ``JurisdictionAdministrator`` and
``JurisdictionSuperAdministrator`` are stored (``role_assignment``). A Judge, a
LegalTextManager and a JurisdictionAdministrator hold their role in one jurisdiction;
the JurisdictionSuperAdministrator is global.

``SystemAdministrator`` is never stored and never offered as a context: it comes only
from the Entra ``roles`` claim, is copied into the BenchNotes token as ``sysadmin`` when
the token is minted, and adds the system-level permissions to whatever the active role
grants, whatever the context, also with no context at all.

``Normal`` is gone (merged into Judge).

Permissions are not in the token: they are derived here from the token's active role
and ``sysadmin`` flag (``permissions_for``), so the map has one home and a change to it
needs no re-issue of tokens. ``recording.any.*`` and ``dictation.any.read`` are granted
to no role; they exist so that a test can prove nobody has them.
"""

from __future__ import annotations

ROLE_JUDGE = "Judge"
ROLE_LEGAL_TEXT_MANAGER = "LegalTextManager"
ROLE_JURISDICTION_ADMIN = "JurisdictionAdministrator"
ROLE_JURISDICTION_SUPER_ADMIN = "JurisdictionSuperAdministrator"
ROLE_SYSTEM_ADMIN = "SystemAdministrator"

# The roles that can be stored, and so held in a context.
STORED_ROLES: tuple[str, ...] = (
    ROLE_JUDGE,
    ROLE_LEGAL_TEXT_MANAGER,
    ROLE_JURISDICTION_ADMIN,
    ROLE_JURISDICTION_SUPER_ADMIN,
)
# Stored roles that apply to every jurisdiction: held with no jurisdiction.
GLOBAL_ROLES: tuple[str, ...] = (ROLE_JURISDICTION_SUPER_ADMIN,)
# Roles a user may ask for (access requests): never an administrator role.
REQUESTABLE_ROLES: tuple[str, ...] = (ROLE_JUDGE, ROLE_LEGAL_TEXT_MANAGER)

# Every permission, in the order the API lists them.
PERMISSIONS: tuple[str, ...] = (
    "app.access",
    "context.switch",
    "access.request",
    "access.approve",
    "dictation.use",
    "recording.upload",
    "recording.own",
    "documents.export",
    "recording.audit.read",
    "recording.any.read",
    "recording.any.edit",
    "recording.any.delete",
    "dictation.any.read",
    "admin.access",
    "legal_text.manage",
    "templates.manage",
    "document_content.read",
    "document_content.write",
    "users.manage",
    "jurisdiction_admin.assign",
    "jurisdiction_super_admin.assign",
    "role_audit.read",
    "jurisdiction.create",
    "jurisdictions.manage",
    "jurisdiction.wording.edit",
    "jurisdiction.features",
    "feature_flags.manage",
    "system.settings",
    "security.alert_test",
)

_COMMON = {"app.access", "context.switch", "access.request"}

_JURISDICTION_ADMIN = _COMMON | {
    "admin.access",
    "users.manage",
    "access.approve",
    "role_audit.read",
    "templates.manage",
    "jurisdictions.manage",
    "jurisdiction.wording.edit",
    "jurisdiction.features",
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_JUDGE: frozenset(
        _COMMON
        | {
            "dictation.use",
            "recording.upload",
            "recording.own",
            "documents.export",
            "recording.audit.read",
            "document_content.read",
        }
    ),
    ROLE_LEGAL_TEXT_MANAGER: frozenset(
        _COMMON
        | {
            "admin.access",
            "legal_text.manage",
            "templates.manage",
            "document_content.read",
            "document_content.write",
        }
    ),
    ROLE_JURISDICTION_ADMIN: frozenset(_JURISDICTION_ADMIN),
    ROLE_JURISDICTION_SUPER_ADMIN: frozenset(_JURISDICTION_ADMIN | {"jurisdiction.create", "jurisdiction_admin.assign"}),
}

# Added by the Entra SystemAdministrator app role, whatever the active context.
SYSTEM_ADMIN_PERMISSIONS: frozenset[str] = frozenset(
    {
        "admin.access",
        "feature_flags.manage",
        "system.settings",
        "security.alert_test",
        "jurisdiction_super_admin.assign",
        "jurisdiction_admin.assign",
    }
)

# Permissions no role has.
UNGRANTED_PERMISSIONS: frozenset[str] = frozenset(PERMISSIONS) - set().union(
    SYSTEM_ADMIN_PERMISSIONS, *ROLE_PERMISSIONS.values()
)


def permissions_for(role: str | None, sysadmin: bool = False) -> list[str]:
    """The permissions of the active ``role`` (None: no active context), plus the
    system-level ones when ``sysadmin``. In ``PERMISSIONS`` order; empty for a role that
    is not a stored role (a token cannot make one up)."""
    granted: set[str] = set(ROLE_PERMISSIONS.get(role or "", ()))
    if sysadmin:
        granted |= SYSTEM_ADMIN_PERMISSIONS
    return [permission for permission in PERMISSIONS if permission in granted]


def role_needs_jurisdiction(role: str) -> bool:
    """Whether the stored role is held within a jurisdiction (all but the global one)."""
    return role in STORED_ROLES and role not in GLOBAL_ROLES
