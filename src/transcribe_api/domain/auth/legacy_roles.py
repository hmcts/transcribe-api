"""The role names the existing route checks read, derived from the BenchNotes token.

Until the central permission checks replace them (DIAAT-376), routes still guard
themselves with ``get_allowlisted_user(required_roles_any=[get_role("Judge"), ...])``, which
compares against ``AuthenticatedUser.app_roles``. They used to be the Entra token's roles;
now they are the active role (the one the user is acting as) plus ``SystemAdministrator``
for a system administrator. No assignment other than the active one grants anything, as
DIAAT-390 requires, and ``Normal`` is never among them.
"""

from __future__ import annotations

from hmcts_azure_auth.roles import get_role

from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.permissions import ROLE_SYSTEM_ADMIN


def legacy_app_roles(ctx: Context | None, sysadmin: bool) -> list[str]:
    """The active role, and SystemAdministrator if the token says so, as the configured
    role values (AUTH_APPROLES may rename the ones the library knows)."""
    roles: list[str] = []
    if ctx is not None:
        roles.append(get_role(ctx.role) or ctx.role)
    if sysadmin:
        roles.append(get_role(ROLE_SYSTEM_ADMIN) or ROLE_SYSTEM_ADMIN)
    return roles
