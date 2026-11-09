"""Load the application user a BenchNotes token names (DIAAT-373)."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlmodel import Session

from transcribe_api.domain.auth.app_token import AppPrincipal
from transcribe_api.domain.auth.context import error
from transcribe_api.domain.models_dictation import User

logger = logging.getLogger(__name__)

USER_DEACTIVATED = "user_deactivated"


def user_deactivated() -> HTTPException:
    return error(403, USER_DEACTIVATED, "This account has been deactivated.")


def load_user(session: Session, principal: AppPrincipal) -> User:
    """The DB user named by the token's ``sub``, for the route's data (the token already
    authorised the request: nothing about what they may do is read here).

    401 if there is no such user (the token outlived the row, or was minted for another
    database); 403 ``user_deactivated`` if they have been deactivated since the token was
    minted, so a deactivation does not wait for the token to expire: the row is being read
    anyway."""
    user = session.get(User, principal.user_id)
    if user is None:
        logger.warning("A BenchNotes token names a user that does not exist: id=%s", principal.user_id)
        raise HTTPException(
            status_code=401, detail="Unknown user", headers={"WWW-Authenticate": "Bearer"}
        )
    if not user.is_active:
        raise user_deactivated()
    return user
