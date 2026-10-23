"""Who owns a dictation upload blob.

Uploads live at ``user-uploads/{owner}/{file}``. Since courtstranscribe 81e6bc7
(2026-07-20) the owner segment is the user's Azure AD object id
(``User.azure_user_id``, see ``stt.utils.get_file_blob_path``); blobs written before
that are keyed by the user's email. Both the API ownership check and the background
poller decide ownership from this segment, so the rules live here and only here.
"""

from sqlalchemy import func
from sqlmodel import Session, select

from transcribe_api.domain.models_dictation import User

USER_UPLOADS_PREFIX = "user-uploads/"

_MIN_PATH_PARTS = 3  # user-uploads/{owner}/{file}


class AmbiguousBlobOwnerError(Exception):
    """A legacy email segment matched more than one user, so the owner cannot be known."""


def blob_owner_segment(blob_path: str) -> str | None:
    """Return the owner segment of a ``user-uploads/{owner}/{file}`` path, or None if malformed."""
    parts = blob_path.split("/")
    if len(parts) < _MIN_PATH_PARTS or parts[0] != USER_UPLOADS_PREFIX.rstrip("/") or not parts[1]:
        return None
    return parts[1]


def is_legacy_email_segment(segment: str) -> bool:
    """Legacy (pre-OID) blobs are keyed by email; an Azure object id never contains '@'."""
    return "@" in segment


def segment_belongs_to_user(segment: str, user: User) -> bool:
    """Whether an owner segment identifies ``user``: by object id, or by email for legacy blobs."""
    if segment == str(user.azure_user_id):
        return True
    return is_legacy_email_segment(segment) and segment.lower() == (user.email or "").lower()


def resolve_blob_owner(session: Session, segment: str) -> User | None:
    """Find the user an owner segment belongs to, by the same rules as ``segment_belongs_to_user``.

    Looks up ``azure_user_id`` first (unique). Only for a segment containing '@' does it
    fall back to a case-insensitive email match; ``User.email`` is not unique, so more
    than one match raises ``AmbiguousBlobOwnerError`` rather than picking one arbitrarily.
    Never creates users.
    """
    user = session.exec(select(User).where(User.azure_user_id == segment)).first()
    if user is not None or not is_legacy_email_segment(segment):
        return user

    matches = session.exec(select(User).where(func.lower(User.email) == segment.lower()).limit(2)).all()
    if len(matches) > 1:
        msg = "Legacy email blob owner matches more than one user"
        raise AmbiguousBlobOwnerError(msg)
    return matches[0] if matches else None
