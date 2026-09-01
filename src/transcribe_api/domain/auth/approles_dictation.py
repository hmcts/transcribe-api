"""App role definitions — re-exported from hmcts-fastapi-azure-auth library."""

from hmcts_azure_auth.roles import DEFAULT_APP_ROLES, get_role, get_valid_roles, has_any_role

__all__ = ["DEFAULT_APP_ROLES", "get_role", "get_valid_roles", "has_any_role"]
