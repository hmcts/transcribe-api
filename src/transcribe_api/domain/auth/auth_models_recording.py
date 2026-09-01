"""Auth models — re-exported from hmcts-fastapi-azure-auth library."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hmcts_azure_auth.models import AuthUser

if TYPE_CHECKING:
    from transcribe_api.domain.models_recording import User

__all__ = ["AuthUser", "AuthenticatedUser"]


@dataclass
class AuthenticatedUser:
    """A DB User paired with the Azure AD roles verified at request time.

    Roles are not persisted — the authoritative source is the JWT on every
    request. Attribute access falls through to the wrapped User for all fields
    not defined on this dataclass, so route code can use current_user.email,
    current_user.id, etc. without changes.
    """

    db_user: User
    app_roles: list[str]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.db_user, name)
