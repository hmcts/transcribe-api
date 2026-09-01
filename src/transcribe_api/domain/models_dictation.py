from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import Column, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, Relationship, SQLModel

# Global config for all models
model_config = {
    "from_attributes": True,
    "extra": "ignore",
    "use_enum_values": True,
}


class TemplateName(StrEnum):
    GENERAL = "General"
    CRISSA = "Crissa"


class BaseTable(SQLModel):
    model_config = {
        "from_attributes": True,
    }

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    created_datetime: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_datetime: datetime | None = Field(default_factory=lambda: datetime.now(UTC))


class DialogueEntry(SQLModel):
    model_config = {
        "from_attributes": True,
    }

    speaker: str
    text: str
    start_time: float
    end_time: float


class TemplateMetadata(SQLModel):
    name: TemplateName
    description: str
    category: Literal["common"]
    beta: bool = False
    is_chronological: bool = False


class MinuteVersion(BaseTable, table=True):
    html_content: str
    template: TemplateMetadata = Field(sa_column=Column(JSONB))
    transcription_id: UUID = Field(foreign_key="transcription.id")
    transcription: "Transcription" = Relationship(back_populates="minute_versions")
    trace_id: str | None = Field(default=None)
    star_rating: int | None = Field(default=None)
    star_rating_comment: str | None = Field(default=None)
    is_generating: bool | None = Field(default=False)
    error_message: str | None = Field(default=None)


# Main models with table=True for DB tables
class User(BaseTable, table=True):
    email: str = Field(index=True)
    azure_user_id: str = Field(unique=True, index=True)
    has_completed_onboarding: bool = Field(default=False)
    role: str = Field(default="Normal", index=True)
    transcriptions: list["Transcription"] = Relationship(back_populates="user")


# Association table for many-to-many relationship between Transcription and Tag
class TranscriptionTagAssociation(SQLModel, table=True):
    """Link model for many-to-many relationship between Transcription and Tag."""

    __tablename__ = "transcription_tag_association"

    transcription_id: UUID = Field(foreign_key="transcription.id", primary_key=True)
    tag_id: UUID = Field(foreign_key="tag.id", primary_key=True)


class Tag(BaseTable, table=True):
    name: str = Field(index=True, unique=True)
    transcriptions: list["Transcription"] = Relationship(
        back_populates="tags",
        link_model=TranscriptionTagAssociation,
    )


class Transcription(BaseTable, table=True):
    user_id: UUID = Field(default=None, foreign_key="user.id")
    user: User | None = Relationship(back_populates="transcriptions")
    title: str | None = Field(default=None)
    minute_versions: list["MinuteVersion"] = Relationship(back_populates="transcription")
    transcription_jobs: list["TranscriptionJob"] = Relationship(back_populates="transcription")
    tags: list["Tag"] = Relationship(
        back_populates="transcriptions",
        link_model=TranscriptionTagAssociation,
    )


class DocumentContent(SQLModel, table=True):
    """Singleton row storing the full document_content JSON.

    Always id=1. All instances read and write the same row via the
    shared PostgreSQL database, replacing the previous per-container
    JSON file approach.
    """

    __tablename__ = "document_content"

    id: int = Field(default=1, primary_key=True)
    content: dict = Field(sa_column=Column(JSONB, nullable=False))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TranscriptionJob(BaseTable, table=True):
    transcription_id: UUID = Field(foreign_key="transcription.id")
    transcription: "Transcription" = Relationship(back_populates="transcription_jobs")
    dialogue_entries: list[DialogueEntry] = Field(sa_column=Column(JSONB))
    error_message: str | None = Field(default=None)
    s3_audio_url: str | None = Field(default=None)
    # Blob deletion cleanup fields
    needs_cleanup: bool = Field(default=False)
    cleanup_failure_reason: str | None = Field(default=None)


class AuditLog(SQLModel, table=True):
    """Immutable record of every access-denied event fired by the auth layer.

    Written by write_audit_event() which is passed as audit_writer to
    get_allowlisted_user() on all privileged routes.
    """

    __tablename__ = "audit_log"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    event_type: str = Field(index=True)
    user_id: str = Field(index=True)
    email: str
    held_roles: list = Field(sa_column=Column(JSONB, nullable=False))
    required_roles: list = Field(sa_column=Column(JSONB, nullable=False))
    resource: str | None = Field(default=None)
    detail: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    client_ip: str | None = Field(default=None)


class LiveTranscriptDraft(BaseTable, table=True):
    """One draft per user — upserted on every debounced save, deleted on submit/restore/discard."""

    __tablename__ = "live_transcript_draft"

    user_id: UUID = Field(foreign_key="user.id", unique=True)
    transcript: dict = Field(sa_column=Column(JSONB, nullable=False))
    form_data: dict = Field(sa_column=Column(JSONB, nullable=False))


# Database helper functions for blob deletion service
async def get_transcription_job_by_id(session: AsyncSession, job_id: UUID) -> TranscriptionJob | None:
    """
    Get a transcription job by its ID.

    Args:
        session: The database session
        job_id: The UUID of the transcription job

    Returns:
        TranscriptionJob | None: The transcription job if found, None otherwise
    """
    stmt = select(TranscriptionJob).where(TranscriptionJob.id == job_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_transcription_jobs_needing_cleanup(session: AsyncSession) -> list[TranscriptionJob]:
    """
    Get all transcription jobs that need manual cleanup.

    Args:
        session: The database session

    Returns:
        list[TranscriptionJob]: List of transcription jobs flagged for manual cleanup
    """
    stmt = select(TranscriptionJob).where(TranscriptionJob.needs_cleanup)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def mark_cleanup_complete(session: AsyncSession, job_id: UUID) -> bool:
    """
    Mark a transcription job as having completed cleanup successfully.

    Args:
        session: The database session
        job_id: The UUID of the transcription job

    Returns:
        bool: True if the job was found and updated, False otherwise
    """
    job = await get_transcription_job_by_id(session, job_id)
    if job:
        job.needs_cleanup = False
        job.cleanup_failure_reason = None
        return True
    return False


async def mark_cleanup_failed(session: AsyncSession, job_id: UUID, error_message: str) -> bool:
    """
    Mark a transcription job as having failed cleanup and flag for manual intervention.

    Args:
        session: The database session
        job_id: The UUID of the transcription job
        error_message: The error message describing the failure

    Returns:
        bool: True if the job was found and updated, False otherwise
    """
    job = await get_transcription_job_by_id(session, job_id)
    if job:
        job.needs_cleanup = True
        job.cleanup_failure_reason = error_message
        return True
    return False
