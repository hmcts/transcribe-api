import pytest

pytest.skip("requires database env vars not available in CI unit tests", allow_module_level=True)

from datetime import UTC, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

from transcribe_api.domain.interface_dictation import _is_transcription_showable  # noqa: E402
from transcribe_api.domain.models_dictation import Transcription, TranscriptionJob  # noqa: E402


def test_showable_for_live_submission_docx_even_with_no_dialogue_entries():
    """Live hearing submissions should appear immediately after save."""
    transcription = Transcription(id=uuid4(), user_id=uuid4(), title="Appellant v Respondent")
    transcription.transcription_jobs = [
        TranscriptionJob(
            transcription_id=transcription.id,
            dialogue_entries=[],
            s3_audio_url="user-uploads/test.user/hearing-document-20260316_090000.docx",
        )
    ]

    assert _is_transcription_showable(transcription, datetime.now(UTC)) is True


def test_not_showable_for_recent_non_docx_job_without_entries():
    """Processing audio jobs without content should still remain hidden initially."""
    transcription = Transcription(id=uuid4(), user_id=uuid4(), title="Untitled")
    transcription.transcription_jobs = [
        TranscriptionJob(
            transcription_id=transcription.id,
            dialogue_entries=[],
            s3_audio_url="user-uploads/test.user/audio-file.wav",
        )
    ]
    transcription.created_datetime = datetime.now(UTC)

    assert _is_transcription_showable(transcription, datetime.now(UTC)) is False
