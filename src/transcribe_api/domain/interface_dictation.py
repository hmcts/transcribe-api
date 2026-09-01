from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytz
import sentry_sdk
from fastapi import HTTPException
from sqlalchemy import event, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from transcribe_api.domain.exceptions import UserHasTranscriptionsError, UserNotFoundError
from transcribe_api.runtime.db_dictation import engine
from transcribe_api.domain.models_dictation import (
    BaseTable,
    DialogueEntry,
    LiveTranscriptDraft,
    MinuteVersion,
    Tag,
    Transcription,
    TranscriptionJob,
    User,
)
from transcribe_api.documents.minutes.types import TranscriptionMetadata


@event.listens_for(Session, "before_flush")
def before_flush(session, flush_context, instances):  # noqa: ARG001
    for obj in session.dirty:
        if isinstance(obj, BaseTable):
            obj.updated_datetime = datetime.now(UTC)


def save_transcription(
    transcription_data: Transcription,
    user_id: UUID,
) -> Transcription:
    with Session(engine) as session:
        transcription_data.user_id = user_id
        merged = session.merge(transcription_data)
        session.commit()
        session.refresh(merged)
        return merged


def create_error_minute_version(
    minute_version_id: str,
    transcription_id: UUID,
    error: Exception,
    template: dict | None = None,
    trace_id: str | None = None,
) -> MinuteVersion:
    """Create an error state to display in UI and end frontend polling.

    Without error minute versions, failed transcriptions would remain
    hidden indefinitely, and the frontend would poll for non-existent
    successful versions.

    Parameters
    ----------
    minute_version_id : str
        Unique ID for this minute version
    transcription_id : UUID
        ID of the parent transcription
    error : Exception
        The error that occurred during processing
    template : dict | None, optional
        Template metadata (omit when reusing existing template from edit operations)
    trace_id : str | None, optional
        LangFuse trace ID for LLM operation debugging (None if error before LLM call)

    Returns
    -------
    MinuteVersion
        Error minute version ready to save to database
    """
    kwargs = {
        "id": minute_version_id,
        "transcription_id": transcription_id,
        "html_content": "",
        "is_generating": False,
        "error_message": str(error),
    }
    if template is not None:
        kwargs["template"] = template
    if trace_id is not None:
        kwargs["trace_id"] = trace_id

    return MinuteVersion(**kwargs)


def save_minute_version(
    minute_data: MinuteVersion,
) -> MinuteVersion:
    with Session(engine) as session:
        minute_data.template = (
            minute_data.template.model_dump() if hasattr(minute_data.template, "model_dump") else minute_data.template
        )
        merged = session.merge(minute_data)
        session.commit()
        session.refresh(merged)
        return merged


def _is_transcription_showable(transcription: Transcription, current_time: datetime) -> bool:  # noqa: PLR0911
    try:
        # Any minute versions have error messages (show for user awareness of errors)
        if transcription.minute_versions and any(
            version.error_message is not None for version in transcription.minute_versions
        ):
            return True

        # Require both General and Crissa templates to be completed before showing
        # This ensures all LLM tasks have completed before displaying to the user
        if transcription.minute_versions:
            completed_templates = {
                version.template["name"] if isinstance(version.template, dict) else version.template.name
                for version in transcription.minute_versions
                if version.html_content and not version.error_message and version.template is not None
            }
            # Only show if both General and Crissa templates are completed
            if "General" in completed_templates and "Crissa" in completed_templates:
                return True

        # Any jobs have error messages (show for user awareness of errors)
        if transcription.transcription_jobs and any(
            job.error_message is not None for job in transcription.transcription_jobs
        ):
            return True

        # Show if there are transcription jobs with dialogue entries (e.g., live hearing submissions)
        if transcription.transcription_jobs and any(job.dialogue_entries for job in transcription.transcription_jobs):
            return True

        # Live hearing submissions always generate a Word document (.docx).
        # They can legitimately have empty dialogue entries (for example when users
        # submit structured notes), so surface them immediately in the UI.
        if transcription.transcription_jobs and any(
            (job.s3_audio_url or "").lower().endswith(".docx") for job in transcription.transcription_jobs
        ):
            return True

        # Created more than 5 minutes ago (safety net to show old incomplete jobs)
        if transcription.created_datetime:
            created_dt = (
                pytz.utc.localize(transcription.created_datetime)
                if transcription.created_datetime.tzinfo is None
                else transcription.created_datetime
            )
            five_minutes_in_seconds = 300
            if (current_time - created_dt).total_seconds() > five_minutes_in_seconds:
                return True

        return False  # noqa: TRY300
    except Exception as e:
        # If anything goes wrong, default to showing the transcription
        sentry_sdk.capture_exception(e)
        return True


def _extract_unique_speakers(transcription: Transcription) -> list[str]:
    """Extract unique speaker names from all transcription jobs."""
    speakers: set[str] = set()

    for job in transcription.transcription_jobs or []:
        for entry in job.dialogue_entries:
            if isinstance(entry, dict):
                speaker_name = entry.get("speaker", "").strip()
            else:
                speaker_name = getattr(entry, "speaker", "").strip()

            if speaker_name:
                speakers.add(speaker_name.title())

    return sorted(speakers)


def fetch_transcriptions_metadata(user_id: UUID, tz) -> list[TranscriptionMetadata]:
    with Session(engine) as session:
        statement = (
            select(Transcription)
            .where(Transcription.user_id == user_id)
            .options(
                selectinload(Transcription.minute_versions),  # type: ignore[arg-type]
                selectinload(Transcription.transcription_jobs),  # type: ignore[arg-type]
                selectinload(Transcription.tags),  # type: ignore[arg-type]
            )
        )
        transcriptions = session.exec(statement).all()

        current_time = datetime.now(UTC)

        return [
            TranscriptionMetadata(
                id=t.id,
                title=t.title or "",
                created_datetime=pytz.utc.localize(t.created_datetime).astimezone(tz),
                updated_datetime=(pytz.utc.localize(t.updated_datetime).astimezone(tz) if t.updated_datetime else None),
                is_showable_in_ui=_is_transcription_showable(t, current_time),
                speakers=_extract_unique_speakers(t),
                tags=[tag.name for tag in (t.tags or [])],
                document_blob_path=t.transcription_jobs[0].s3_audio_url
                if t.transcription_jobs and t.transcription_jobs[0].s3_audio_url
                else None,
            )
            for t in transcriptions
        ]


def get_transcription_by_id(transcription_id: UUID, user_id: UUID, tz) -> Transcription:
    with Session(engine) as session:
        statement = (
            select(Transcription)
            .where(
                Transcription.id == transcription_id,
                Transcription.user_id == user_id,
            )
            .options(
                selectinload(Transcription.transcription_jobs),  # type: ignore[arg-type]
                selectinload(Transcription.tags),  # type: ignore[arg-type]
            )
        )
        transcription = session.exec(statement).first()
        if not transcription:
            raise HTTPException(status_code=404, detail="Transcription not found")

        # Convert the date to local timezone
        if transcription.created_datetime:
            transcription.created_datetime = pytz.utc.localize(transcription.created_datetime).astimezone(tz)
        if transcription.updated_datetime:
            transcription.updated_datetime = pytz.utc.localize(transcription.updated_datetime).astimezone(tz)

        return transcription


def delete_transcription_by_id(
    transcription_id: UUID,
    user_id: UUID,
) -> None:
    with Session(engine) as session:
        statement = select(Transcription).where(
            Transcription.id == transcription_id,
            Transcription.user_id == user_id,
        )
        transcription = session.exec(statement).first()

        if not transcription:
            raise HTTPException(status_code=404, detail="Transcription not found")

        minute_versions = get_minute_versions(transcription_id)
        for version in minute_versions:
            session.delete(version)

        transcription_jobs = get_transcription_jobs(transcription_id)
        for job in transcription_jobs:
            session.delete(job)

        session.delete(transcription)
        session.commit()


def get_minute_versions(
    transcription_id: UUID,
) -> list[MinuteVersion]:
    with Session(engine) as session:
        # First verify the transcription exists
        transcription = session.get(Transcription, transcription_id)
        if not transcription:
            raise HTTPException(status_code=404, detail="Transcription not found")

        statement = select(MinuteVersion).where(MinuteVersion.transcription_id == transcription_id)
        results = session.exec(statement).all()
        return list(results)


def get_minute_version_by_id(
    minute_version_id: UUID,
    transcription_id: UUID,
) -> MinuteVersion:
    minute_versions = get_minute_versions(transcription_id)
    for version in minute_versions:
        if UUID(str(version.id)) == UUID(str(minute_version_id)):
            return version
    raise HTTPException(status_code=404, detail="Minute version not found")


def save_transcription_job(
    job: TranscriptionJob,
) -> TranscriptionJob:
    with Session(engine) as session:
        job.dialogue_entries = [
            entry.model_dump() if hasattr(entry, "model_dump") else entry for entry in job.dialogue_entries
        ]
        merged = session.merge(job)
        session.commit()
        session.refresh(merged)
        return merged


def get_transcription_jobs(
    transcription_id: UUID,
) -> list[TranscriptionJob]:
    with Session(engine) as session:
        transcription = session.get(Transcription, transcription_id)
        if not transcription:
            raise HTTPException(status_code=404, detail="Transcription not found")

        statement = select(TranscriptionJob).where(TranscriptionJob.transcription_id == transcription_id)
        results = session.exec(statement).all()

        for job in results:
            job.dialogue_entries = [DialogueEntry(**entry) for entry in job.dialogue_entries]

        return list(results)


def get_user_by_id(user_id: UUID) -> User:
    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user


def update_user(user_id: UUID, **kwargs) -> User:
    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        for key, value in kwargs.items():
            if hasattr(user, key):
                setattr(user, key, value)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def mark_user_onboarding_complete(user_id: UUID) -> User:
    """Mark user as having completed onboarding"""
    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.has_completed_onboarding = True
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def get_tags_for_transcription(transcription_id: UUID, user_id: UUID) -> list[Tag]:
    """Get all tags for a transcription."""
    with Session(engine) as session:
        # Verify transcription exists and belongs to user
        transcription = session.get(Transcription, transcription_id)
        if not transcription or transcription.user_id != user_id:
            raise HTTPException(status_code=404, detail="Transcription not found")

        # Load tags with selectinload
        statement = (
            select(Transcription).where(Transcription.id == transcription_id).options(selectinload(Transcription.tags))  # type: ignore[arg-type]
        )
        transcription_with_tags = session.exec(statement).first()
        if not transcription_with_tags:
            raise HTTPException(status_code=404, detail="Transcription not found")

        return list(transcription_with_tags.tags or [])


def get_or_create_tag(tag_name: str) -> Tag:
    """Get an existing tag by name or create a new one."""
    with Session(engine) as session:
        # Normalize tag name (trim and lowercase for consistency)
        normalized_name = tag_name.strip().lower()

        # Try to find existing tag
        statement = select(Tag).where(Tag.name == normalized_name)
        existing_tag = session.exec(statement).first()

        if existing_tag:
            return existing_tag

        # Create new tag
        new_tag = Tag(name=normalized_name)
        session.add(new_tag)
        session.commit()
        session.refresh(new_tag)
        return new_tag


def add_tag_to_transcription(transcription_id: UUID, tag_name: str, user_id: UUID) -> Tag:
    """Add a tag to a transcription."""
    with Session(engine) as session:
        # Verify transcription exists and belongs to user
        transcription = session.get(Transcription, transcription_id)
        if not transcription or transcription.user_id != user_id:
            raise HTTPException(status_code=404, detail="Transcription not found")

        # Get or create the tag
        tag = get_or_create_tag(tag_name)

        # Reload transcription with tags relationship
        statement = (
            select(Transcription).where(Transcription.id == transcription_id).options(selectinload(Transcription.tags))  # type: ignore[arg-type]
        )
        transcription_with_tags = session.exec(statement).first()
        if not transcription_with_tags:
            raise HTTPException(status_code=404, detail="Transcription not found")

        # Add tag if not already present
        if tag not in (transcription_with_tags.tags or []):
            if transcription_with_tags.tags is None:
                transcription_with_tags.tags = []
            transcription_with_tags.tags.append(tag)
            session.add(transcription_with_tags)
            session.commit()

        return tag


def remove_tag_from_transcription(transcription_id: UUID, tag_name: str, user_id: UUID) -> None:
    """Remove a tag from a transcription."""
    with Session(engine) as session:
        # Verify transcription exists and belongs to user
        transcription = session.get(Transcription, transcription_id)
        if not transcription or transcription.user_id != user_id:
            raise HTTPException(status_code=404, detail="Transcription not found")

        # Normalize tag name
        normalized_name = tag_name.strip().lower()

        # Find the tag
        tag_statement = select(Tag).where(Tag.name == normalized_name)
        tag = session.exec(tag_statement).first()
        if not tag:
            raise HTTPException(status_code=404, detail="Tag not found")

        # Reload transcription with tags relationship
        statement = (
            select(Transcription).where(Transcription.id == transcription_id).options(selectinload(Transcription.tags))  # type: ignore[arg-type]
        )
        transcription_with_tags = session.exec(statement).first()
        if not transcription_with_tags:
            raise HTTPException(status_code=404, detail="Transcription not found")

        # Remove tag if present
        if transcription_with_tags.tags and tag in transcription_with_tags.tags:
            transcription_with_tags.tags.remove(tag)
            session.add(transcription_with_tags)
            session.commit()


# ---------------------------------------------------------------------------
# Admin functions — bypass ownership filter
# ---------------------------------------------------------------------------


def admin_get_transcription_by_id(transcription_id: UUID) -> Transcription:
    """Fetch a single transcription by ID with no user ownership check."""
    with Session(engine) as session:
        statement = (
            select(Transcription)
            .where(Transcription.id == transcription_id)
            .options(
                selectinload(Transcription.transcription_jobs),  # type: ignore[arg-type]
                selectinload(Transcription.tags),  # type: ignore[arg-type]
            )
        )
        transcription = session.exec(statement).first()
        if not transcription:
            raise HTTPException(status_code=404, detail="Transcription not found")
        return transcription


def admin_fetch_all_transcriptions(
    *,
    owner_email: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Transcription], int]:
    """List all transcriptions across all users with optional filters and pagination."""
    if page < 1:
        raise ValueError(f"page must be >= 1, got {page}")
    if page_size < 1 or page_size > 200:
        raise ValueError(f"page_size must be between 1 and 200, got {page_size}")

    with Session(engine) as session:
        filters = []
        if owner_email:
            filters.append(User.email == owner_email)
        if created_after:
            filters.append(Transcription.created_datetime >= created_after)
        if created_before:
            filters.append(Transcription.created_datetime <= created_before)

        count_stmt = (
            select(func.count(Transcription.id))  # type: ignore[arg-type]
            .join(User, Transcription.user_id == User.id)  # type: ignore[arg-type]
        )
        for f in filters:
            count_stmt = count_stmt.where(f)
        total = session.exec(count_stmt).one()

        data_stmt = (
            select(Transcription)
            .join(User, Transcription.user_id == User.id)  # type: ignore[arg-type]
            .options(
                selectinload(Transcription.user),  # type: ignore[arg-type]
                selectinload(Transcription.transcription_jobs),  # type: ignore[arg-type]
                selectinload(Transcription.tags),  # type: ignore[arg-type]
            )
            .order_by(Transcription.created_datetime.desc())  # type: ignore[arg-type]
        )
        for f in filters:
            data_stmt = data_stmt.where(f)
        data_stmt = data_stmt.offset((page - 1) * page_size).limit(page_size)
        transcriptions = session.exec(data_stmt).all()

        return list(transcriptions), total
_LIVE_DRAFT_MAX_AGE = timedelta(hours=24)


def get_live_draft(user_id: UUID) -> LiveTranscriptDraft | None:
    with Session(engine) as session:
        draft = session.exec(
            select(LiveTranscriptDraft).where(LiveTranscriptDraft.user_id == user_id)
        ).first()
        if draft is None:
            return None
        saved_at = draft.updated_datetime or draft.created_datetime
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=UTC)
        if datetime.now(UTC) - saved_at > _LIVE_DRAFT_MAX_AGE:
            session.delete(draft)
            session.commit()
            return None
        return draft


def upsert_live_draft(user_id: UUID, transcript: dict, form_data: dict) -> None:
    now = datetime.now(UTC)
    stmt = (
        pg_insert(LiveTranscriptDraft)
        .values(
            id=uuid4(),
            user_id=user_id,
            transcript=transcript,
            form_data=form_data,
            created_datetime=now,
            updated_datetime=now,
        )
        .on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                "transcript": transcript,
                "form_data": form_data,
                "updated_datetime": now,
            },
        )
    )
    with Session(engine) as session:
        session.execute(stmt)
        session.commit()


def delete_live_draft(user_id: UUID) -> None:
    with Session(engine) as session:
        draft = session.exec(
            select(LiveTranscriptDraft).where(LiveTranscriptDraft.user_id == user_id)
        ).first()
        if draft:
            session.delete(draft)
            session.commit()


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------


def admin_list_users(
    *,
    page: int = 1,
    page_size: int = 50,
    search_email: str | None = None,
) -> tuple[list[User], int]:
    """Return a paginated list of all users with an optional email substring filter."""
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be >= 1")
    if page_size < 1 or page_size > 200:
        raise HTTPException(status_code=422, detail="page_size must be between 1 and 200")

    with Session(engine) as session:
        filters = []
        if search_email:
            filters.append(User.email.ilike(f"%{search_email}%"))

        count_stmt = select(func.count(User.id))  # type: ignore[arg-type]
        for f in filters:
            count_stmt = count_stmt.where(f)
        total = session.exec(count_stmt).one()

        data_stmt = select(User)
        for f in filters:
            data_stmt = data_stmt.where(f)
        data_stmt = data_stmt.offset((page - 1) * page_size).limit(page_size)
        users = list(session.exec(data_stmt).all())
        return users, total


def admin_delete_user(user_id: UUID) -> None:
    """Delete a user after verifying they have no transcriptions.

    Raises UserNotFoundError if the user does not exist.
    Raises UserHasTranscriptionsError if the user still owns transcriptions.
    Automatically removes any live draft belonging to the user before deletion.
    """
    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user:
            raise UserNotFoundError(user_id)

        transcription_count = session.exec(
            select(func.count(Transcription.id)).where(Transcription.user_id == user_id)  # type: ignore[arg-type]
        ).one()
        if transcription_count > 0:
            raise UserHasTranscriptionsError(user_id, transcription_count)

        draft = session.exec(
            select(LiveTranscriptDraft).where(LiveTranscriptDraft.user_id == user_id)
        ).first()
        if draft:
            session.delete(draft)

        session.delete(user)
        session.commit()
