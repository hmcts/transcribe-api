"""Domain exceptions for the database layer.

Raised by DB functions instead of HTTPException so the DB layer stays
decoupled from FastAPI. Route handlers catch these and convert them to the
appropriate HTTP status codes.
"""

from __future__ import annotations

from uuid import UUID


class UserNotFoundError(Exception):
    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id
        super().__init__(f"User {user_id} not found")


class UserHasTranscriptionsError(Exception):
    def __init__(self, user_id: UUID, count: int) -> None:
        self.user_id = user_id
        self.count = count
        super().__init__(
            f"Cannot delete user {user_id}: they own {count} transcription(s). "
            "Delete or reassign those transcriptions before removing the user."
        )


class OwnershipConflictError(Exception):
    """A save would move an existing record to a different owner (SEC-1).

    Saves use ``session.merge``, which updates whatever row has the given ID. IDs
    can come from the client (request bodies, upload file names), so a save must
    never re-parent an existing row. The message carries no IDs.
    """

    def __init__(self, record_type: str) -> None:
        self.record_type = record_type
        super().__init__(f"{record_type} belongs to a different owner")
