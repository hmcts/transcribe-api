"""Unit tests for minutes types module."""

import uuid
from datetime import UTC, datetime


class TestTranscriptionMetadataSerializeDatetime:
    def test_serializes_datetime_to_isoformat(self):
        from transcribe_api.documents.minutes.types import TranscriptionMetadata

        dt = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        meta = TranscriptionMetadata(
            id=uuid.uuid4(),
            title="Test",
            created_datetime=dt,
            is_showable_in_ui=True,
        )
        data = meta.model_dump(mode="json")
        assert data["created_datetime"] == dt.isoformat()

    def test_serializes_none_datetime_to_none(self):
        from transcribe_api.documents.minutes.types import TranscriptionMetadata

        dt = datetime(2026, 1, 15, tzinfo=UTC)
        meta = TranscriptionMetadata(
            id=uuid.uuid4(),
            title="Test",
            created_datetime=dt,
            updated_datetime=None,
            is_showable_in_ui=False,
        )
        data = meta.model_dump(mode="json")
        assert data["updated_datetime"] is None
