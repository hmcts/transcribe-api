"""The system feature flags, for a SystemAdministrator (DIAAT-392), under ``/api/admin``.

``GET /api/admin/feature-flags`` lists them and ``PUT /api/admin/feature-flags/{name}`` sets one;
both need ``feature_flags.manage`` (the SystemAdministrator's, whatever the context). The first
flag is ``dictation_enabled``. A change is audited as ``feature_flag.changed`` with the flag, the
value it had, the value it has now and the roles the caller acted as; the process that took the
change clears its flag cache, so ``GET /api/flags`` and the dictation off-switch see it at once
(other replicas within ``feature_flags.CACHE_TTL_SECONDS``). Plain defs: the work is synchronous,
so FastAPI runs them in the threadpool.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, StrictBool
from sqlmodel import Session

from transcribe_api.domain import feature_flags
from transcribe_api.domain.auth import context as auth_context
from transcribe_api.domain.auth.context import error
from transcribe_api.domain.auth.dependencies_dictation import require_permission
from transcribe_api.domain.auth.permissions import ROLE_SYSTEM_ADMIN
from transcribe_api.domain.models_dictation import User
from transcribe_api.runtime import db_connection

router = APIRouter(prefix="/admin", tags=["admin"])


class FeatureFlagOut(BaseModel):
    name: str
    value: bool
    description: str
    updated_at: datetime | None
    # The email of whoever last set it (a SystemAdministrator reads this); null while it has its default.
    updated_by: str | None


class FeatureFlagsResponse(BaseModel):
    flags: list[FeatureFlagOut]


class FeatureFlagWrite(BaseModel):
    # Strict: "true", 1 and null are refused (422), not coerced.
    value: StrictBool


def _flags(session: Session) -> FeatureFlagsResponse:
    return FeatureFlagsResponse(
        flags=[
            FeatureFlagOut(
                name=state.name,
                value=state.value,
                description=state.description,
                updated_at=state.updated_at,
                updated_by=state.updated_by,
            )
            for state in feature_flags.list_flags(session)
        ]
    )


@router.get("/feature-flags", response_model=FeatureFlagsResponse)
def list_feature_flags(
    response: Response,
    _user: User = Depends(require_permission("feature_flags.manage")),  # noqa: B008
) -> FeatureFlagsResponse:
    """Every system feature flag with its effective value (its stored value, else its
    configured default), its description, and when and by whom it was last set."""
    response.headers["Cache-Control"] = "no-store"
    with Session(db_connection.get_engine()) as session:
        return _flags(session)


@router.put("/feature-flags/{name}", response_model=FeatureFlagsResponse)
def set_feature_flag(
    name: str,
    body: FeatureFlagWrite,
    response: Response,
    user: User = Depends(require_permission("feature_flags.manage")),  # noqa: B008
) -> FeatureFlagsResponse:
    """Set a flag (``{"value": true|false}``) and get the flags back, as the GET lists them.
    404 ``flag_not_found`` for a name that is not a flag. Audited with from, to and the roles
    the caller acted as."""
    response.headers["Cache-Control"] = "no-store"
    if name not in feature_flags.FLAG_DEFAULTS:
        raise error(404, "flag_not_found", "There is no such feature flag.")
    ctx = auth_context.context_of(user)
    with Session(db_connection.get_engine()) as session:
        acted_as = [
            role
            for role in (ctx.role if ctx else None, ROLE_SYSTEM_ADMIN if auth_context.sysadmin_of(user) else None)
            if role
        ]
        # Audited in the same transaction as the change (fail-closed: domain/feature_flags.set_flag).
        feature_flags.set_flag(session, name, body.value, updated_by=user.id, acted_as=acted_as)
        return _flags(session)
