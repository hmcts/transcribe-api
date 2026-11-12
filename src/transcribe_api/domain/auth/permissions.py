"""BenchNotes roles and permissions (DIAAT-373, DIAAT-376): the one role -> permission map.

Roles
-----
``Judge``, ``LegalTextManager``, ``JurisdictionAdministrator`` and
``JurisdictionSuperAdministrator`` are stored (``role_assignment``). A Judge, a
LegalTextManager and a JurisdictionAdministrator hold their role in one jurisdiction;
the JurisdictionSuperAdministrator is global.

``SystemAdministrator`` is never stored: it comes only from the Entra ``roles`` claim and is
copied into the BenchNotes token as ``sysadmin`` when the token is minted. A holder is offered
it as a context of its own (no jurisdiction), and has the system-level permissions only while
acting in it (DIAAT-403): acting as any other role, they have that role's permissions and no
more, like anyone else. It is invisible to everyone else (never assignable, never listed).

A JurisdictionAdministrator is also offered ``Judge`` and ``LegalTextManager`` as contexts in their own
jurisdiction (``ROLES_INCLUDED_WITH_ADMIN``): derived when assignments are read
(domain/assignments.py), not stored.

``Normal`` is gone (merged into Judge).

Permissions are not in the token: they are derived here from the token's active role
(``permissions_for``), so the map has one home and a change to it
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
# The roles a context can be acted in: the stored ones, and SystemAdministrator for a holder of
# the Entra role (with no jurisdiction).
CONTEXT_ROLES: tuple[str, ...] = (*STORED_ROLES, ROLE_SYSTEM_ADMIN)
# Stored roles that apply to every jurisdiction: held with no jurisdiction.
GLOBAL_ROLES: tuple[str, ...] = (ROLE_JURISDICTION_SUPER_ADMIN,)
# Roles a JurisdictionAdministrator holds in their jurisdiction without being given them (DIAAT-373):
# derived at read time from the administrator assignment, never stored, so removing the administrator
# role (or leaving the jurisdiction) removes them too. Not for the global JurisdictionSuperAdministrator
# or the SystemAdministrator, who are not tied to one jurisdiction.
ROLES_INCLUDED_WITH_ADMIN: tuple[str, ...] = (ROLE_JUDGE, ROLE_LEGAL_TEXT_MANAGER)
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

# The SystemAdministrator context's: only while acting as SystemAdministrator.
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

# Permissions that apply to a jurisdiction (DIAAT-391, DIAAT-394): held by a
# JurisdictionAdministrator (or a LegalTextManager, for templates and legal text) only in the
# jurisdiction they are acting in, and by the JurisdictionSuperAdministrator in every one.
# Checked by ``authorisation.assert_jurisdiction_in_scope`` wherever a route names the
# jurisdiction it works on.
JURISDICTION_SCOPED_PERMISSIONS: frozenset[str] = frozenset(
    {
        "users.manage",
        "jurisdictions.manage",
        "jurisdiction.wording.edit",
        "jurisdiction.features",
        "templates.manage",
        "access.approve",
        "role_audit.read",
    }
)

# Permissions no role has.
UNGRANTED_PERMISSIONS: frozenset[str] = frozenset(PERMISSIONS) - set().union(
    SYSTEM_ADMIN_PERMISSIONS, *ROLE_PERMISSIONS.values()
)


def permissions_for(role: str | None, sysadmin: bool = False) -> list[str]:
    """The permissions of the active ``role`` only (None: no active context, none). The
    SystemAdministrator role's need ``sysadmin``, the Entra role, as well. In ``PERMISSIONS``
    order; empty for a role that is not a context role (a token cannot make one up)."""
    if role == ROLE_SYSTEM_ADMIN:
        granted: frozenset[str] = SYSTEM_ADMIN_PERMISSIONS if sysadmin else frozenset()
    else:
        granted = ROLE_PERMISSIONS.get(role or "", frozenset())
    return [permission for permission in PERMISSIONS if permission in granted]


def role_needs_jurisdiction(role: str) -> bool:
    """Whether the stored role is held within a jurisdiction (all but the global one)."""
    return role in STORED_ROLES and role not in GLOBAL_ROLES


# Who may assign which stored roles (contract s1, "Assignment rules"): the roles a caller
# acting as ``role`` may give or take away. A caller acting as SystemAdministrator may assign the
# two administrator roles and, on themselves only, any stored role; Phase 4 enforces the
# rules per request; this is the list GET /admin/roles shows.
_ASSIGNABLE_BY_ROLE: dict[str, tuple[str, ...]] = {
    ROLE_JURISDICTION_ADMIN: (ROLE_JUDGE, ROLE_LEGAL_TEXT_MANAGER),
    ROLE_JURISDICTION_SUPER_ADMIN: (ROLE_JUDGE, ROLE_LEGAL_TEXT_MANAGER, ROLE_JURISDICTION_ADMIN),
}
_ASSIGNABLE_BY_SYSADMIN: tuple[str, ...] = (ROLE_JURISDICTION_ADMIN, ROLE_JURISDICTION_SUPER_ADMIN)


def assignable_roles(role: str | None, sysadmin: bool = False) -> list[str]:
    """The stored roles a caller acting as ``role`` may assign to others, in ``STORED_ROLES``
    order (acting as SystemAdministrator needs ``sysadmin`` too). Never SystemAdministrator:
    nobody can grant it, and it is not a stored role."""
    if role == ROLE_SYSTEM_ADMIN:
        granted: tuple[str, ...] = _ASSIGNABLE_BY_SYSADMIN if sysadmin else ()
    else:
        granted = _ASSIGNABLE_BY_ROLE.get(role or "", ())
    return [r for r in STORED_ROLES if r in granted]
