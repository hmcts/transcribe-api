"""First-login User creation shared by the dictation and recording resolvers."""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from transcribe_api.domain.models_dictation import User

logger = logging.getLogger(__name__)


def insert_user_or_load_existing(session: Session, user: User) -> User:
    """Insert a new User, or return the row a concurrent request inserted first.

    Two first-login requests for the same user can both miss the lookup and both
    insert; ix_user_azure_user_id rejects the second. The session must be the
    caller's own short-lived one: rolling it back discards only the failed insert.
    """
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.exec(select(User).where(User.azure_user_id == user.azure_user_id)).first()
        if existing is None:
            # Not the race (e.g. ck_user_role_valid): there is no row to fall back on.
            raise
        logger.info("Concurrent first login; loaded existing user id=%s", existing.id)
        return existing

    session.refresh(user)
    logger.info("Created new user id=%s", user.id)
    return user
