import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from transcribe_api.domain.models_dictation import (
    TemplateMetadata,
)


class TemplateResponse(BaseModel):
    templates: list[TemplateMetadata]


class TranscriptionMetadata(BaseModel):
    """Pydantic model for transcription metadata."""

    model_config = ConfigDict()

    id: uuid.UUID
    title: str
    created_datetime: datetime
    updated_datetime: datetime | None = None
    is_showable_in_ui: bool
    speakers: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    document_blob_path: str | None = None

    @field_serializer("created_datetime", "updated_datetime")
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return value.isoformat() if value else None


class GenerateMinutesRequest(BaseModel):
    transcription_id: uuid.UUID
    template: TemplateMetadata
    edit_instructions: str | None = None
    current_minute_version_id: uuid.UUID | None = None
    action_type: Literal["generate", "edit"]


class UploadUrlRequest(BaseModel):
    file_extension: str


class SpeakerPrediction(BaseModel):
    original_speaker: str
    predicted_name: str
    confidence: float


class SpeakerPredictionOutput(BaseModel):
    predictions: list[SpeakerPrediction]


class MeetingTitleOutput(BaseModel):
    title: str = Field(description="the title of the meeting")


class UploadUrlResponse(BaseModel):
    upload_url: str
    user_upload_s3_file_key: str
    # transcription_id: uuid.UUID


class PlaybackUrlResponse(BaseModel):
    playback_url: str


class StartTranscriptionJobRequest(BaseModel):
    user_upload_s3_file_key: str
    transcription_id: str | None = None


class UpdateUserRequest(BaseModel):
    hide_citations: bool | None = None
    # Add other user fields here as needed


class OnboardingStatusResponse(BaseModel):
    """
    Response model for user onboarding status and allowlist check.

    This model contains all the information needed by the frontend to determine
    the user's access level and what UI to display.

    Attributes
    ----------
    has_completed_onboarding : bool
        Whether the user has completed the onboarding process.
    force_onboarding_override : bool
        Whether onboarding is being forced in development mode.
    should_show_onboarding : bool
        Whether the onboarding UI should be displayed to the user.
    user_id : uuid.UUID
        Unique identifier for the user.
    environment : str
        Current environment (local, dev, prod, etc.).
    is_allowlisted : bool
        Whether the user's email is in the allowlist.
    should_show_coming_soon : bool
        Whether the "coming soon" page should be displayed instead of the app.
    """

    has_completed_onboarding: bool
    force_onboarding_override: bool
    should_show_onboarding: bool
    user_id: uuid.UUID
    environment: str
    is_allowlisted: bool
    should_show_coming_soon: bool


class TranscriptEntry(BaseModel):
    """A single entry in a real-time transcript."""

    speaker: str
    text: str
    timestamp: str


class GenerateSummaryRequest(BaseModel):
    """Request model for generating real-time summaries."""

    transcript_entries: list[TranscriptEntry]
    existing_summary: str | None = None


class GenerateSummaryResponse(BaseModel):
    """Response model for generated summaries."""

    summary: str


class AdminTranscriptionRecord(BaseModel):
    id: uuid.UUID
    title: str | None = None
    created_datetime: datetime | None = None
    owner_id: uuid.UUID
    owner_email: str

    @field_serializer("created_datetime")
    def serialize_admin_datetime(self, value: datetime | None) -> str | None:
        return value.isoformat() if value else None


class AdminTranscriptionListResponse(BaseModel):
    items: list[AdminTranscriptionRecord]
    total: int
    page: int
    page_size: int
