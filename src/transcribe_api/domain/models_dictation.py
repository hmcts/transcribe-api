from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, LargeBinary, UniqueConstraint, select, text
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
    # index: dictation migration a8f2c9d5e1b3 (15-20s p95 latency fix); restored by 0002
    transcription_id: UUID = Field(foreign_key="transcription.id", index=True)
    transcription: "Transcription" = Relationship(back_populates="minute_versions")
    trace_id: str | None = Field(default=None)
    star_rating: int | None = Field(default=None)
    star_rating_comment: str | None = Field(default=None)
    is_generating: bool | None = Field(default=False)
    error_message: str | None = Field(default=None)


# Main models with table=True for DB tables
class User(BaseTable, table=True):
    # Was only in dictation migration 8e2a5d9f3b7c; declared here so the model
    # and the migration baseline describe the same schema.
    __table_args__ = (
        CheckConstraint(
            "role IN ('Normal', 'Judge', 'LegalTextManager', 'SystemAdministrator')",
            name="ck_user_role_valid",
        ),
    )

    email: str = Field(index=True)
    azure_user_id: str = Field(unique=True, index=True)
    has_completed_onboarding: bool = Field(default=False)
    # Retired (DIAAT-382): nothing reads or writes it any more; a user's roles are their
    # role_assignment rows. NULL for users created since 0009 (a new user has no role).
    # Dropped in a later release, with jurisdiction_membership and jurisdiction.auto_enrol.
    role: str | None = Field(default=None, index=True)
    # Retired (DIAAT-390): the active jurisdiction is chosen per session (the BenchNotes
    # token's ctx), not remembered here. Dropped in a later release.
    active_jurisdiction_id: UUID | None = Field(default=None, foreign_key="jurisdiction.id")
    # 0009 (DIAAT-382): display name from the sign-in (Entra ``name`` claim).
    name: str | None = Field(default=None)
    # 0009: a deactivated user is refused at sign-in and never re-enabled by it.
    is_active: bool = Field(default=True)
    transcriptions: list["Transcription"] = Relationship(back_populates="user")


class Jurisdiction(SQLModel, table=True):
    """A jurisdiction using BenchNotes (DIAAT-322), e.g. the IAC or the ET (0004).

    A row, not code: adding a jurisdiction needs no deployment. ``settings`` holds its
    screen wording and what it uses (domain/jurisdictions.JurisdictionSettings).
    """

    __tablename__ = "jurisdiction"
    __table_args__ = (CheckConstraint("stage IN ('draft', 'test', 'live')", name="ck_jurisdiction_stage"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    # Short, stable, upper case ("IAC", "ET"): what the web app sends in X-Jurisdiction.
    code: str = Field(unique=True, index=True, max_length=16)
    name: str
    # What users see, e.g. "Employment Tribunals (ET)"; None: the name (DIAAT-345).
    display_name: str | None = Field(default=None)
    # The service it is for, where that applies (DIAAT-345).
    service: str | None = Field(default=None)
    # A disabled jurisdiction is invisible to its members, as if they had none.
    enabled: bool = Field(default=True)
    # Who of its members sees it (DIAAT-345, 0007): draft, its admins; test, its
    # testers and admins; live, all of them. The admin area creates drafts; a row
    # made without a stage is live, as every jurisdiction was before stages.
    stage: str = Field(default="live", max_length=8, sa_column_kwargs={"server_default": text("'live'")})
    # Retired (DIAAT-373): nothing reads it, and the cutover script sets it false. A new
    # user has no role until an administrator adds them or approves their request.
    auto_enrol: bool = Field(default=False)
    settings: dict = Field(sa_column=Column(JSONB, nullable=False))
    created_datetime: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))
    updated_datetime: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))


class UploadJurisdiction(SQLModel, table=True):
    """The jurisdiction an upload was started in (DIAAT-322, 0004).

    Written when the upload URL is issued, from the tab's jurisdiction; read by the
    poller, which has no request, when it makes the upload's transcription. Keyed by
    the upload's ID, which becomes the transcription's. A hearing recording's upload
    is recorded too (its ID, in the blob name): its job must be submitted in the same
    jurisdiction (DIAAT-334).
    """

    __tablename__ = "upload_jurisdiction"

    transcription_id: UUID = Field(primary_key=True)
    jurisdiction_id: UUID = Field(foreign_key="jurisdiction.id")
    created_datetime: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))


class JurisdictionMembership(SQLModel, table=True):
    """A user's role in a jurisdiction (DIAAT-322, 0004). Retired by DIAAT-382: nothing
    reads it since role_assignment (0009, backfilled from this table) took over; dropped
    in a later release.

    ``user_id`` is NULL for someone added by email before their first sign-in: the row
    is claimed (user_id set) when they first sign in (domain/jurisdictions.py).
    """

    __tablename__ = "jurisdiction_membership"
    __table_args__ = (
        UniqueConstraint("jurisdiction_id", "user_id", name="uq_jurisdiction_membership_user"),
        Index(
            "uq_jurisdiction_membership_pending_email",
            "jurisdiction_id",
            "email",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
            sqlite_where=text("user_id IS NULL"),
        ),
        CheckConstraint("role IN ('judge', 'admin')", name="ck_jurisdiction_membership_role"),
        CheckConstraint("user_id IS NOT NULL OR email IS NOT NULL", name="ck_jurisdiction_membership_who"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    jurisdiction_id: UUID = Field(foreign_key="jurisdiction.id", index=True)
    user_id: UUID | None = Field(default=None, foreign_key="user.id", index=True)
    # Lower case. Only used to claim a pending membership on first sign-in.
    email: str | None = Field(default=None)
    role: str = Field(default="judge", max_length=16)
    # In the jurisdiction's test group: sees it while it is in test (DIAAT-345).
    tester: bool = Field(default=False, sa_column_kwargs={"server_default": text("false")})
    created_datetime: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))


# The roles a role_assignment may hold: SystemAdministrator (Entra only) and the retired
# default role can never be stored (DIAAT-382).
STORED_ROLES_SQL = "'Judge', 'LegalTextManager', 'JurisdictionAdministrator', 'JurisdictionSuperAdministrator'"


class RoleAssignment(SQLModel, table=True):
    """A user's role in a jurisdiction (DIAAT-382, 0009): the source of truth for what a
    user may be, replacing user.role and jurisdiction_membership (and its ``tester`` flag).

    One row per (user, jurisdiction, role). Judge, LegalTextManager and
    JurisdictionAdministrator are held in a jurisdiction; the JurisdictionSuperAdministrator
    is global (``jurisdiction_id`` NULL). ``user_id`` is NULL for someone added by email
    before their first sign-in (``pending_email``, lower case): the row is claimed
    (``user_id`` set, ``pending_email`` cleared) when they first sign in
    (domain/assignments.py).

    Uniqueness is two partial indexes per holder, as a NULL jurisdiction is never equal
    to another in a unique index: one for a jurisdiction role, one for the global role.
    """

    __tablename__ = "role_assignment"
    __table_args__ = (
        CheckConstraint(f"role IN ({STORED_ROLES_SQL})", name="ck_role_assignment_role"),
        CheckConstraint(
            "(role = 'JurisdictionSuperAdministrator' AND jurisdiction_id IS NULL)"
            " OR (role <> 'JurisdictionSuperAdministrator' AND jurisdiction_id IS NOT NULL)",
            name="ck_role_assignment_jurisdiction",
        ),
        CheckConstraint("user_id IS NOT NULL OR pending_email IS NOT NULL", name="ck_role_assignment_who"),
        CheckConstraint(
            "pending_email IS NULL OR pending_email = lower(pending_email)", name="ck_role_assignment_email_lower"
        ),
        Index(
            "uq_role_assignment_user_jurisdiction_role",
            "user_id",
            "jurisdiction_id",
            "role",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL AND jurisdiction_id IS NOT NULL"),
            sqlite_where=text("user_id IS NOT NULL AND jurisdiction_id IS NOT NULL"),
        ),
        Index(
            "uq_role_assignment_user_global_role",
            "user_id",
            "role",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL AND jurisdiction_id IS NULL"),
            sqlite_where=text("user_id IS NOT NULL AND jurisdiction_id IS NULL"),
        ),
        Index(
            "uq_role_assignment_pending_jurisdiction_role",
            "pending_email",
            "jurisdiction_id",
            "role",
            unique=True,
            postgresql_where=text("pending_email IS NOT NULL AND jurisdiction_id IS NOT NULL"),
            sqlite_where=text("pending_email IS NOT NULL AND jurisdiction_id IS NOT NULL"),
        ),
        Index(
            "uq_role_assignment_pending_global_role",
            "pending_email",
            "role",
            unique=True,
            postgresql_where=text("pending_email IS NOT NULL AND jurisdiction_id IS NULL"),
            sqlite_where=text("pending_email IS NOT NULL AND jurisdiction_id IS NULL"),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    user_id: UUID | None = Field(default=None, foreign_key="user.id", index=True)
    # Lower case. Only used to claim a pending assignment on first sign-in.
    pending_email: str | None = Field(default=None, index=True)
    jurisdiction_id: UUID | None = Field(default=None, foreign_key="jurisdiction.id", index=True)
    role: str = Field(max_length=32)
    # NULL for a row the migration made from a membership, or made by a script.
    granted_by: UUID | None = Field(default=None, foreign_key="user.id")
    granted_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))
    # In the jurisdiction's test group (DIAAT-345): of a non-administrator's row, it lets them see
    # the jurisdiction while it is at stage "test" (an administrator always sees it).
    tester: bool = Field(default=False, sa_column_kwargs={"server_default": text("false")})


ACCESS_REQUEST_STATUSES = ("pending", "approved", "rejected", "withdrawn")


class AccessRequest(SQLModel, table=True):
    """A user's request for roles in a jurisdiction (DIAAT-396, 0009), approved or
    rejected by a JurisdictionAdministrator of it or a JurisdictionSuperAdministrator.
    The roles asked for are ``access_request_role`` rows."""

    __tablename__ = "access_request"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'withdrawn')", name="ck_access_request_status"
        ),
        Index("ix_access_request_jurisdiction_status", "jurisdiction_id", "status"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    # The requester.
    user_id: UUID = Field(foreign_key="user.id", index=True)
    jurisdiction_id: UUID = Field(foreign_key="jurisdiction.id")
    status: str = Field(default="pending", max_length=16)
    decided_by: UUID | None = Field(default=None, foreign_key="user.id")
    decided_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    reason: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))


class AccessRequestRole(SQLModel, table=True):
    """A role asked for in an access request: only Judge or LegalTextManager (DIAAT-396)."""

    __tablename__ = "access_request_role"
    __table_args__ = (
        CheckConstraint("role IN ('Judge', 'LegalTextManager')", name="ck_access_request_role_role"),
    )

    access_request_id: UUID = Field(foreign_key="access_request.id", primary_key=True)
    role: str = Field(primary_key=True, max_length=32)


class SystemFeatureFlag(SQLModel, table=True):
    """A system-wide switch a SystemAdministrator sets (DIAAT-392, 0009). A flag with no
    row has its default (from configuration)."""

    __tablename__ = "system_feature_flag"

    name: str = Field(primary_key=True, max_length=64)
    value: bool
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))
    updated_by: UUID | None = Field(default=None, foreign_key="user.id")


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
    # index: dictation migration a8f2c9d5e1b3; restored by 0002
    user_id: UUID = Field(default=None, foreign_key="user.id", index=True)
    user: User | None = Relationship(back_populates="transcriptions")
    # DIAAT-322 (0004): the jurisdiction it was made in. Set by the server only.
    jurisdiction_id: UUID | None = Field(default=None, foreign_key="jurisdiction.id", index=True)
    title: str | None = Field(default=None)
    minute_versions: list["MinuteVersion"] = Relationship(back_populates="transcription")
    transcription_jobs: list["TranscriptionJob"] = Relationship(back_populates="transcription")
    tags: list["Tag"] = Relationship(
        back_populates="transcriptions",
        link_model=TranscriptionTagAssociation,
    )


class JurisdictionTemplate(SQLModel, table=True):
    """A Word template uploaded for a jurisdiction (DIAAT-344, 0008).

    Its file, the boxes found in it and the field linked to each box
    (documents/uploaded_templates.py). Judges of the jurisdiction choose it on the
    recording page once it is available to them; it is only made available with every
    box linked.
    """

    __tablename__ = "jurisdiction_template"
    __table_args__ = (UniqueConstraint("jurisdiction_id", "name", name="uq_jurisdiction_template_name"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    jurisdiction_id: UUID = Field(foreign_key="jurisdiction.id", index=True, nullable=False)
    # What judges choose it by, e.g. "Judgment"; unique in its jurisdiction.
    name: str = Field(max_length=200)
    # The service it is for, where that applies (shown beside its name).
    service: str | None = Field(default=None, max_length=200)
    # The uploaded file's name, for the admin area only.
    file_name: str = Field(max_length=255)
    content: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    sha256: str = Field(max_length=64)
    # [{key, label, kind, options, count}]: what the file has (uploaded_templates.Box).
    boxes: list = Field(sa_column=Column(JSONB, nullable=False))
    # [{tag, label, type, required, options, source}], in form order (TemplateField).
    fields: list = Field(sa_column=Column(JSONB, nullable=False))
    # Judges can choose it. Never with a box unlinked.
    enabled: bool = Field(default=False)
    created_datetime: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))
    updated_datetime: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))


class DocumentContent(SQLModel, table=True):
    """A jurisdiction's legal wording (DIAAT-342): hearing types, legal frameworks and
    anonymity text, as the JSON the admin area edits. One row per jurisdiction.

    The IAC's row is the one that always existed: id 1, which stays the IAC's (id 1 is
    reserved for it: ``id`` is generated from 2, so no other jurisdiction can take it,
    and anything that reads id 1, the live-edit drift check among it, still finds the
    IAC's wording). A jurisdiction has no row until its first save: until then it has
    the default wording without legal frameworks (documents/content_loader.py).
    """

    __tablename__ = "document_content"
    # Declared as 0006 makes it (PostgreSQL only: SQLite stores a UUID without dashes).
    __table_args__ = (
        CheckConstraint(
            "id <> 1 OR jurisdiction_id = '6a1c0f3e-3b7d-4c2a-9e51-1ac000000001'", name="ck_document_content_id_1_is_iac"
        ).ddl_if(dialect="postgresql"),
    )

    id: int | None = Field(default=None, primary_key=True)
    # 0006: NOT NULL, one row per jurisdiction, defaulting to the IAC so that a pod still
    # running the previous release, which inserts id 1 and names no jurisdiction, stays
    # correct while a release rolls out.
    jurisdiction_id: UUID = Field(foreign_key="jurisdiction.id", unique=True)
    content: dict = Field(sa_column=Column(JSONB, nullable=False))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))


class TranscriptionJob(BaseTable, table=True):
    # index: dictation migration a8f2c9d5e1b3; restored by 0002
    transcription_id: UUID = Field(foreign_key="transcription.id", index=True)
    transcription: "Transcription" = Relationship(back_populates="transcription_jobs")
    dialogue_entries: list[DialogueEntry] = Field(sa_column=Column(JSONB))
    error_message: str | None = Field(default=None)
    s3_audio_url: str | None = Field(default=None)
    # DIAAT-286: the configuration that produced this job's output (a decision
    # document or a transcript), shaped by domain/provenance_dictation.Provenance. No content.
    # Set by the server only (0003): a client's value is ignored (save_transcription_job).
    provenance: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # Blob deletion cleanup fields
    needs_cleanup: bool = Field(default=False)
    cleanup_failure_reason: str | None = Field(default=None)


class AuditLog(SQLModel, table=True):
    """Immutable record of a security-relevant event (see app/database/audit.py).

    Access denied by the auth layer (written by write_audit_event, passed as
    audit_writer of the permission checks, domain/auth/authorisation.py), role changes, user deletions and, since
    DIAAT-286, the sensitive actions on records: viewed, exported, deleted and
    ownership refusals, with their outcome.
    """

    __tablename__ = "audit_log"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True, sa_type=DateTime(timezone=True))
    # The action: an hmcts_azure_auth value (ACCESS_DENIED, ROLE_CHANGE) or an app
    # action, <object>.<verb> (runtime/telemetry_schema.AUDIT_ACTIONS).
    event_type: str = Field(index=True)
    # The actor. App rows: the internal user UUID (user.id). Rows from the auth
    # library: the Azure object ID.
    user_id: str = Field(index=True)
    # Written by the auth library only; app rows identify the actor by user_id (0003).
    email: str | None = Field(default=None)
    # App rows have no roles: an empty list (NOT NULL).
    held_roles: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    required_roles: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    resource: str | None = Field(default=None)
    # none_as_null: no detail is SQL NULL, not a JSON null (as recording's table had).
    detail: dict | None = Field(default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True))
    client_ip: str | None = Field(default=None)
    # DIAAT-286 (0003): success, denied or failed. NULL on older auth-library rows.
    outcome: str | None = Field(default=None, max_length=16)
    # What the action was on: a security resource type, transcript or sectioned_transcript.
    object_type: str | None = Field(default=None, max_length=32)
    # A record UUID as text, never a title or file name.
    object_id: str | None = Field(default=None, max_length=64, index=True)
    # hmcts.request.id: joins the row to the request's telemetry.
    request_id: str | None = Field(default=None, max_length=64, index=True)
    # 0009 (DIAAT-390): the jurisdiction and role the actor was acting in. NULL on older
    # rows, on a global role's rows (no jurisdiction) and where no context was chosen.
    active_jurisdiction_id: UUID | None = Field(default=None)
    active_role: str | None = Field(default=None, max_length=32)


class LiveTranscriptDraft(BaseTable, table=True):
    """One draft per user per jurisdiction — upserted on every debounced save, deleted on
    submit/restore/discard."""

    __tablename__ = "live_transcript_draft"
    __table_args__ = (
        UniqueConstraint("user_id", "jurisdiction_id", name="uq_live_transcript_draft_user_jurisdiction"),
    )

    # timestamptz, unlike the other dictation tables (as in the old schema).
    created_datetime: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True))
    updated_datetime: datetime | None = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )

    user_id: UUID = Field(foreign_key="user.id")
    # DIAAT-322 (0004): a draft belongs to the jurisdiction it was dictated in.
    jurisdiction_id: UUID = Field(foreign_key="jurisdiction.id")
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
