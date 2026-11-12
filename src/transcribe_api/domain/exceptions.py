"""Domain exceptions for the database layer.

Raised by DB functions instead of HTTPException so the DB layer stays
decoupled from FastAPI. Route handlers catch these and convert them to the
appropriate HTTP status codes.
"""

from __future__ import annotations


class JurisdictionConflictError(Exception):
    """DIAAT-334: a save names the caller's record of another jurisdiction."""

    def __init__(self, record_type: str) -> None:
        super().__init__(f"{record_type} belongs to another jurisdiction")
        self.record_type = record_type


class OwnershipConflictError(Exception):
    """A save would move an existing record to a different owner (SEC-1).

    Saves use ``session.merge``, which updates whatever row has the given ID. IDs
    can come from the client (request bodies, upload file names), so a save must
    never re-parent an existing row. The message carries no IDs.
    """

    def __init__(self, record_type: str) -> None:
        self.record_type = record_type
        super().__init__(f"{record_type} belongs to a different owner")
