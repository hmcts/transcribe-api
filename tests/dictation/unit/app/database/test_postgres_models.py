"""Unit tests for postgres model helper functions."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


@pytest.mark.asyncio
class TestGetTranscriptionJobById:
    async def test_returns_job_when_found(self):
        from transcribe_api.domain.models_dictation import get_transcription_job_by_id

        mock_job = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        job_id = uuid4()
        result = await get_transcription_job_by_id(session, job_id)
        assert result is mock_job

    async def test_returns_none_when_not_found(self):
        from transcribe_api.domain.models_dictation import get_transcription_job_by_id

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        result = await get_transcription_job_by_id(session, uuid4())
        assert result is None


@pytest.mark.asyncio
class TestGetTranscriptionJobsNeedingCleanup:
    async def test_returns_list_of_jobs(self):
        from transcribe_api.domain.models_dictation import get_transcription_jobs_needing_cleanup

        mock_jobs = [MagicMock(), MagicMock()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = mock_jobs

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        result = await get_transcription_jobs_needing_cleanup(session)
        assert result == mock_jobs

    async def test_returns_empty_list_when_none(self):
        from transcribe_api.domain.models_dictation import get_transcription_jobs_needing_cleanup

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        result = await get_transcription_jobs_needing_cleanup(session)
        assert result == []


@pytest.mark.asyncio
class TestMarkCleanupComplete:
    async def test_returns_true_and_clears_flags_when_job_found(self):
        from transcribe_api.domain.models_dictation import mark_cleanup_complete

        mock_job = MagicMock()
        mock_job.needs_cleanup = True
        mock_job.cleanup_failure_reason = "some error"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        result = await mark_cleanup_complete(session, uuid4())
        assert result is True
        assert mock_job.needs_cleanup is False
        assert mock_job.cleanup_failure_reason is None

    async def test_returns_false_when_job_not_found(self):
        from transcribe_api.domain.models_dictation import mark_cleanup_complete

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        result = await mark_cleanup_complete(session, uuid4())
        assert result is False


@pytest.mark.asyncio
class TestMarkCleanupFailed:
    async def test_returns_true_and_sets_flags_when_job_found(self):
        from transcribe_api.domain.models_dictation import mark_cleanup_failed

        mock_job = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        result = await mark_cleanup_failed(session, uuid4(), "Blob deletion failed")
        assert result is True
        assert mock_job.needs_cleanup is True
        assert mock_job.cleanup_failure_reason == "Blob deletion failed"

    async def test_returns_false_when_job_not_found(self):
        from transcribe_api.domain.models_dictation import mark_cleanup_failed

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        result = await mark_cleanup_failed(session, uuid4(), "error")
        assert result is False
