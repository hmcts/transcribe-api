"""Auth models — re-exported from hmcts-fastapi-azure-auth library."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hmcts_azure_auth.models import AuthUser

from transcribe_api.domain.auth.app_token import Context

if TYPE_CHECKING:
    from transcribe_api.domain.models_dictation import User

__all__ = ["AuthUser", "AuthenticatedUser"]


@dataclass
class AuthenticatedUser:
    """A DB User paired with what the caller's BenchNotes token says (DIAAT-373).

    ``ctx`` is the (jurisdiction, role) the user is acting in, None until chosen;
    ``assignments`` are the pairs they hold; ``sysadmin`` is the Entra SystemAdministrator
    role, copied into the token when it was minted. What they may do is derived from ``ctx`` and
    ``sysadmin`` (domain/auth/authorisation.py).
    Attribute access falls through to the wrapped User for all fields not defined on this
    dataclass, so route code can use current_user.email, current_user.id, etc. without
    changes.
    """

    db_user: User
    ctx: Context | None = None
    sysadmin: bool = False
    assignments: tuple[Context, ...] = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.db_user, name)
