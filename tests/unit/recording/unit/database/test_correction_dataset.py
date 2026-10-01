"""Unit tests for record_correction_dataset_entry (DIAAT-231).

Verifies the write path is gated behind CORRECTIONS_DATASET_EXPORT_ENABLED
(default False) and, when enabled, stages a CorrectionDatasetEntry capturing
original/corrected text, confidence, job/segment context, and a timestamp.
"""

import uuid
from unittest.mock import MagicMock

from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.domain.interface_recording import record_correction_dataset_entry
from transcribe_api.domain.models_recording import CorrectionDatasetEntry, JobStatus, SpeechBatchJob


def _make_job() -> SpeechBatchJob:
    return SpeechBatchJob(
        id=uuid.uuid4(),
        caller_id=uuid.uuid4(),
        audio_url="https://storage.example.com/audio.wav",
        locale="en-GB",
        status=JobStatus.SUCCEEDED,
    )


class TestRecordCorrectionDatasetEntry:
    def test_does_nothing_when_flag_disabled(self, monkeypatch):
        monkeypatch.setenv("CORRECTIONS_DATASET_EXPORT_ENABLED", "false")
        get_settings.cache_clear()
        session = MagicMock()
        job = _make_job()

        record_correction_dataset_entry(
            session,
            job=job,
            segment_index=0,
            correction_kind="segment",
            original_text="the quick brown fox",
            corrected_text="the slow brown fox",
            confidence=0.9,
            speaker="0",
        )

        session.add.assert_not_called()

    def test_stages_a_row_when_flag_enabled(self, monkeypatch):
        monkeypatch.setenv("CORRECTIONS_DATASET_EXPORT_ENABLED", "true")
        get_settings.cache_clear()
        session = MagicMock()
        job = _make_job()

        record_correction_dataset_entry(
            session,
            job=job,
            segment_index=2,
            correction_kind="word_range",
            original_text="quick",
            corrected_text="slow",
            confidence=0.75,
            speaker="1",
            start_word_index=1,
            end_word_index=1,
        )

        session.add.assert_called_once()
        (record,) = session.add.call_args.args
        assert isinstance(record, CorrectionDatasetEntry)
        assert record.job_id == job.id
        assert record.caller_id == job.caller_id
        assert record.segment_index == 2
        assert record.correction_kind == "word_range"
        assert record.original_text == "quick"
        assert record.corrected_text == "slow"
        assert record.confidence == 0.75
        assert record.speaker == "1"
        assert record.start_word_index == 1
        assert record.end_word_index == 1
        assert record.locale == "en-GB"
        # created_datetime is set eagerly by BaseTable's default_factory,
        # doubling as the correction's recorded timestamp.
        assert record.created_datetime is not None

    def test_does_not_commit_itself(self, monkeypatch):
        """Staging is add-only; the caller commits alongside the job update."""
        monkeypatch.setenv("CORRECTIONS_DATASET_EXPORT_ENABLED", "true")
        get_settings.cache_clear()
        session = MagicMock()
        job = _make_job()

        record_correction_dataset_entry(
            session,
            job=job,
            segment_index=0,
            correction_kind="segment",
            original_text="a",
            corrected_text="b",
            confidence=None,
            speaker="0",
        )

        session.commit.assert_not_called()
