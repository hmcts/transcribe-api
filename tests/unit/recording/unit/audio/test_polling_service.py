"""Unit tests for BatchPollingService."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

_JOB_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_CALLER_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_JOB_URL = "https://eastus.cognitiveservices.azure.com/speechtotext/transcriptions/abc"
_WEBHOOK_SECRET = "test-secret"


def _make_pending_job(batch_status="NotStarted", callback_url="https://cb.example.com/hook"):
    from transcribe_api.stt.polling_service import _PendingJob
    from transcribe_api.domain.models_recording import BatchJobStatus

    return _PendingJob(
        id=_JOB_ID,
        batch_job_url=_JOB_URL,
        batch_job_status=BatchJobStatus(batch_status),
        callback_url=callback_url,
        caller_id=_CALLER_ID,
        metadata_={"ref": "test"},
        webhook_secret=_WEBHOOK_SECRET,
    )


@pytest.fixture
def service():
    from transcribe_api.stt.polling_service import BatchPollingService

    svc = BatchPollingService(webhook_secret_resolver=lambda caller_id: _WEBHOOK_SECRET)
    return svc


class TestExtractModelIdentifier:
    def test_uses_azure_model_self_url_when_present(self):
        from transcribe_api.stt.polling_service import _extract_model_identifier

        status_data = {"model": {"self": "https://eastus.example.com/models/base/xyz"}}
        assert (
            _extract_model_identifier(status_data, "en-GB")
            == "https://eastus.example.com/models/base/xyz"
        )

    def test_falls_back_to_locale_label_when_model_missing(self):
        from transcribe_api.stt.polling_service import _extract_model_identifier

        assert _extract_model_identifier({}, "en-GB") == "azure-speech-batch-transcription (en-GB)"

    def test_falls_back_when_model_self_is_blank(self):
        from transcribe_api.stt.polling_service import _extract_model_identifier

        status_data = {"model": {"self": ""}}
        assert (
            _extract_model_identifier(status_data, "fr-FR")
            == "azure-speech-batch-transcription (fr-FR)"
        )

    def test_falls_back_when_model_is_not_a_dict(self):
        from transcribe_api.stt.polling_service import _extract_model_identifier

        # Azure (or a mock) could return a non-object here; must not raise.
        for bad in ("some-string", ["a", "b"], 123, None):
            assert (
                _extract_model_identifier({"model": bad}, "en-GB")
                == "azure-speech-batch-transcription (en-GB)"
            )


class TestExtractTranscriptionDurationSeconds:
    def test_uses_azure_timestamps_when_both_present(self):
        from transcribe_api.stt.polling_service import (
            _extract_transcription_duration_seconds,
        )

        status_data = {
            "createdDateTime": "2026-07-15T10:00:00Z",
            "lastActionDateTime": "2026-07-15T10:01:30Z",
        }
        assert _extract_transcription_duration_seconds(status_data, None) == 90.0

    def test_falls_back_to_wall_clock_when_azure_timestamps_missing(self):
        from transcribe_api.stt.polling_service import (
            _extract_transcription_duration_seconds,
        )

        started = datetime.now(UTC) - timedelta(seconds=30)
        duration = _extract_transcription_duration_seconds({}, started)
        assert duration is not None
        assert 29.0 <= duration <= 35.0

    def test_falls_back_when_azure_timestamps_unparseable(self):
        from transcribe_api.stt.polling_service import (
            _extract_transcription_duration_seconds,
        )

        started = datetime.now(UTC) - timedelta(seconds=10)
        status_data = {"createdDateTime": "not-a-date", "lastActionDateTime": "also-not-a-date"}
        duration = _extract_transcription_duration_seconds(status_data, started)
        assert duration is not None
        assert 9.0 <= duration <= 15.0

    def test_returns_none_when_nothing_available(self):
        from transcribe_api.stt.polling_service import (
            _extract_transcription_duration_seconds,
        )

        assert _extract_transcription_duration_seconds({}, None) is None

    def test_wall_clock_fallback_never_returns_negative_under_clock_skew(self):
        from transcribe_api.stt.polling_service import (
            _extract_transcription_duration_seconds,
        )

        # Clock skew: the job-creation timestamp is ahead of the worker's
        # clock (fallback_start in the future). The wall-clock difference is
        # negative, but a duration must never be negative — clamp to 0.
        future_start = datetime.now(UTC) + timedelta(seconds=60)
        duration = _extract_transcription_duration_seconds({}, future_start)
        assert duration == 0.0

    def test_azure_timestamp_path_never_returns_negative(self):
        from transcribe_api.stt.polling_service import (
            _extract_transcription_duration_seconds,
        )

        # lastActionDateTime before createdDateTime (skew/reordering): the
        # Azure path rejects the negative window and falls back — here with
        # no fallback_start it returns None rather than a negative number.
        status_data = {
            "createdDateTime": "2026-07-15T10:01:30Z",
            "lastActionDateTime": "2026-07-15T10:00:00Z",
        }
        assert _extract_transcription_duration_seconds(status_data, None) is None


class TestComposeModelDisplayName:
    def test_combines_display_name_and_locale(self):
        from transcribe_api.stt.polling_service import _compose_model_display_name

        result = _compose_model_display_name({"displayName": "20240614 Base", "locale": "en-GB"})
        assert result == "20240614 Base — en-GB"

    def test_uses_display_name_only_when_locale_absent(self):
        from transcribe_api.stt.polling_service import _compose_model_display_name

        assert _compose_model_display_name({"displayName": "Base Model"}) == "Base Model"

    def test_returns_none_when_display_name_missing(self):
        from transcribe_api.stt.polling_service import _compose_model_display_name

        assert _compose_model_display_name({"locale": "en-GB"}) is None

    def test_returns_none_when_display_name_blank(self):
        from transcribe_api.stt.polling_service import _compose_model_display_name

        assert _compose_model_display_name({"displayName": "   "}) is None

    def test_returns_none_when_display_name_not_a_string(self):
        from transcribe_api.stt.polling_service import _compose_model_display_name

        assert _compose_model_display_name({"displayName": 123}) is None

    def test_ignores_non_string_locale(self):
        from transcribe_api.stt.polling_service import _compose_model_display_name

        assert _compose_model_display_name({"displayName": "Base", "locale": 5}) == "Base"


class TestResolveModelDisplayName:
    @pytest.fixture(autouse=True)
    def _pin_speech_endpoint(self, monkeypatch):
        # The SSRF guard compares the model URL host against
        # AZURE_SPEECH_ENDPOINT; pin it so these tests don't depend on global
        # env state that other test modules may have mutated.
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "https://test.cognitiveservices.azure.com")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_resolves_via_speech_api_when_identifier_is_url(self):
        from transcribe_api.stt.polling_service import _resolve_model_display_name

        with patch(
            "transcribe_api.stt.polling_service.get_model_details",
            new_callable=AsyncMock,
            return_value={"displayName": "20240614 Base", "locale": "en-GB"},
        ) as mock_get:
            result = await _resolve_model_display_name(
                "https://test.cognitiveservices.azure.com/speechtotext/v3.2/models/base/abc"
            )

        assert result == "20240614 Base — en-GB"
        mock_get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_resolution_for_non_url_identifier(self):
        from transcribe_api.stt.polling_service import _resolve_model_display_name

        with patch(
            "transcribe_api.stt.polling_service.get_model_details",
            new_callable=AsyncMock,
        ) as mock_get:
            result = await _resolve_model_display_name("azure-speech-batch-transcription (en-GB)")

        assert result is None
        mock_get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_resolution_for_untrusted_host(self):
        """The subscription key must never be sent off our Speech host: a URL
        on any other host is rejected without dereferencing (SSRF guard)."""
        from transcribe_api.stt.polling_service import _resolve_model_display_name

        with patch(
            "transcribe_api.stt.polling_service.get_model_details",
            new_callable=AsyncMock,
        ) as mock_get:
            result = await _resolve_model_display_name(
                "https://attacker.example.com/speechtotext/v3.2/models/base/abc"
            )

        assert result is None
        mock_get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_resolution_for_non_https_url(self):
        """Plain http is rejected even on the right host — the key only travels
        over TLS."""
        from transcribe_api.stt.polling_service import _resolve_model_display_name

        with patch(
            "transcribe_api.stt.polling_service.get_model_details",
            new_callable=AsyncMock,
        ) as mock_get:
            result = await _resolve_model_display_name(
                "http://test.cognitiveservices.azure.com/speechtotext/v3.2/models/base/abc"
            )

        assert result is None
        mock_get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_is_best_effort_when_speech_api_fails(self, caplog):
        import logging

        from transcribe_api.stt.polling_service import _resolve_model_display_name

        with (
            patch(
                "transcribe_api.stt.polling_service.get_model_details",
                new_callable=AsyncMock,
                side_effect=Exception("401 Unauthorized"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = await _resolve_model_display_name(
                "https://test.cognitiveservices.azure.com/speechtotext/v3.2/models/base/abc"
            )

        assert result is None
        assert "Could not resolve model display name" in caplog.text


class TestProcessJob:
    @pytest.mark.asyncio
    async def test_updates_status_to_running_when_azure_reports_running(self, service):
        job = _make_pending_job(batch_status="NotStarted")
        status_data = {"status": "Running"}

        with (
            patch(
                "transcribe_api.stt.polling_service.get_batch_job_status",
                new_callable=AsyncMock,
                return_value=status_data,
            ),
            patch.object(service, "_update_status") as mock_update,
        ):
            await service._process_job(job)

        from transcribe_api.domain.models_recording import BatchJobStatus

        mock_update.assert_called_once_with(_JOB_ID, BatchJobStatus.RUNNING)

    @pytest.mark.asyncio
    async def test_dispatches_to_handle_succeeded(self, service):
        job = _make_pending_job(batch_status="Running")
        status_data = {"status": "Succeeded"}

        with (
            patch(
                "transcribe_api.stt.polling_service.get_batch_job_status",
                new_callable=AsyncMock,
                return_value=status_data,
            ),
            patch.object(service, "_handle_succeeded", new_callable=AsyncMock) as mock_succeeded,
        ):
            await service._process_job(job)

        mock_succeeded.assert_awaited_once_with(job, status_data)

    @pytest.mark.asyncio
    async def test_dispatches_to_handle_failed(self, service):
        job = _make_pending_job()
        status_data = {"status": "Failed", "properties": {"error": {"message": "speech error"}}}

        with (
            patch(
                "transcribe_api.stt.polling_service.get_batch_job_status",
                new_callable=AsyncMock,
                return_value=status_data,
            ),
            patch.object(service, "_handle_failed", new_callable=AsyncMock) as mock_failed,
        ):
            await service._process_job(job)

        mock_failed.assert_awaited_once_with(job, status_data)


class TestHandleSucceeded:
    @pytest.fixture(autouse=True)
    def _pin_speech_endpoint(self, monkeypatch):
        # Model-name resolution's SSRF guard compares the model.self host
        # against AZURE_SPEECH_ENDPOINT; pin it so these tests are robust to
        # global env state mutated by other test modules.
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "https://test.cognitiveservices.azure.com")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_dispatches_webhook_on_success(self, service):
        job = _make_pending_job()
        mock_entries = [MagicMock()]

        with (
            patch(
                "transcribe_api.stt.polling_service.get_batch_results",
                new_callable=AsyncMock,
                return_value=mock_entries,
            ),
            patch(
                "transcribe_api.stt.polling_service.process_speakers",
                return_value=mock_entries,
            ),
            patch.object(service, "_save_results"),
            patch.object(service, "_dispatch_success", new_callable=AsyncMock) as mock_dispatch,
            patch(
                "transcribe_api.stt.polling_service.delete_batch_job",
                new_callable=AsyncMock,
            ),
        ):
            await service._handle_succeeded(job, {"status": "Succeeded"})

        mock_dispatch.assert_awaited_once_with(job, mock_entries)

    @pytest.mark.asyncio
    async def test_saves_model_identifier_and_duration_from_azure_response(self, service):
        job = _make_pending_job()
        mock_entries = [MagicMock()]
        status_data = {
            "status": "Succeeded",
            "model": {"self": "https://test.cognitiveservices.azure.com/models/base/abc123"},
            "createdDateTime": "2026-07-15T09:00:00Z",
            "lastActionDateTime": "2026-07-15T09:00:42Z",
        }

        with (
            patch(
                "transcribe_api.stt.polling_service.get_batch_results",
                new_callable=AsyncMock,
                return_value=mock_entries,
            ),
            patch(
                "transcribe_api.stt.polling_service.process_speakers",
                return_value=mock_entries,
            ),
            patch(
                "transcribe_api.stt.polling_service.get_model_details",
                new_callable=AsyncMock,
                return_value={"displayName": "20240614 Base", "locale": "en-GB"},
            ),
            patch.object(service, "_save_results") as mock_save,
            patch.object(service, "_dispatch_success", new_callable=AsyncMock),
            patch(
                "transcribe_api.stt.polling_service.delete_batch_job",
                new_callable=AsyncMock,
            ),
        ):
            await service._handle_succeeded(job, status_data)

        from transcribe_api.domain.models_recording import BatchJobStatus

        mock_save.assert_called_once_with(
            job.id,
            mock_entries,
            BatchJobStatus.SUCCEEDED,
            42.0,
            "https://test.cognitiveservices.azure.com/models/base/abc123",
            "20240614 Base — en-GB",
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_locale_label_when_azure_omits_model(self, service):
        job = _make_pending_job()
        mock_entries = [MagicMock()]

        with (
            patch(
                "transcribe_api.stt.polling_service.get_batch_results",
                new_callable=AsyncMock,
                return_value=mock_entries,
            ),
            patch(
                "transcribe_api.stt.polling_service.process_speakers",
                return_value=mock_entries,
            ),
            patch.object(service, "_save_results") as mock_save,
            patch.object(service, "_dispatch_success", new_callable=AsyncMock),
            patch(
                "transcribe_api.stt.polling_service.delete_batch_job",
                new_callable=AsyncMock,
            ),
        ):
            await service._handle_succeeded(job, {"status": "Succeeded"})

        model_identifier = mock_save.call_args[0][4]
        assert model_identifier == "azure-speech-batch-transcription (en-GB)"

    @pytest.mark.asyncio
    async def test_model_resolution_failure_does_not_break_completion(self, service):
        """A failed model-name resolution is best-effort: the job still saves
        (with model_display_name=None) and the success webhook still fires."""
        job = _make_pending_job()
        mock_entries = [MagicMock()]
        status_data = {
            "status": "Succeeded",
            "model": {"self": "https://test.cognitiveservices.azure.com/models/base/abc123"},
        }

        with (
            patch(
                "transcribe_api.stt.polling_service.get_batch_results",
                new_callable=AsyncMock,
                return_value=mock_entries,
            ),
            patch(
                "transcribe_api.stt.polling_service.process_speakers",
                return_value=mock_entries,
            ),
            patch(
                "transcribe_api.stt.polling_service.get_model_details",
                new_callable=AsyncMock,
                side_effect=Exception("network error"),
            ),
            patch.object(service, "_save_results") as mock_save,
            patch.object(service, "_dispatch_success", new_callable=AsyncMock) as mock_dispatch,
            patch(
                "transcribe_api.stt.polling_service.delete_batch_job",
                new_callable=AsyncMock,
            ),
        ):
            await service._handle_succeeded(job, status_data)

        # model_display_name (6th positional arg) is None, but the raw
        # identifier (5th) is still persisted and the webhook still fires.
        assert mock_save.call_args[0][4] == (
            "https://test.cognitiveservices.azure.com/models/base/abc123"
        )
        assert mock_save.call_args[0][5] is None
        mock_dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_marks_needs_cleanup_when_delete_fails(self, service):
        job = _make_pending_job()
        mock_entries = [MagicMock()]

        with (
            patch(
                "transcribe_api.stt.polling_service.get_batch_results",
                new_callable=AsyncMock,
                return_value=mock_entries,
            ),
            patch(
                "transcribe_api.stt.polling_service.process_speakers",
                return_value=mock_entries,
            ),
            patch.object(service, "_save_results"),
            patch.object(service, "_dispatch_success", new_callable=AsyncMock),
            patch(
                "transcribe_api.stt.polling_service.delete_batch_job",
                new_callable=AsyncMock,
                side_effect=Exception("Azure error"),
            ),
            patch.object(service, "_record_cleanup_failure") as mock_cleanup,
        ):
            await service._handle_succeeded(job, {"status": "Succeeded"})

        mock_cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatches_failure_webhook_when_results_unavailable(self, service):
        job = _make_pending_job()

        with (
            patch(
                "transcribe_api.stt.polling_service.get_batch_results",
                new_callable=AsyncMock,
                side_effect=Exception("BatchResultError"),
            ),
            patch.object(service, "_record_error"),
            patch.object(service, "_dispatch_failure", new_callable=AsyncMock) as mock_fail,
        ):
            await service._handle_succeeded(job, {"status": "Succeeded"})

        mock_fail.assert_awaited_once()


class TestHandleFailed:
    @pytest.mark.asyncio
    async def test_records_error_and_dispatches_webhook(self, service):
        job = _make_pending_job()
        status_data = {"properties": {"error": {"message": "Azure transcription failed"}}}

        with (
            patch.object(service, "_record_error") as mock_error,
            patch.object(service, "_dispatch_failure", new_callable=AsyncMock) as mock_dispatch,
        ):
            await service._handle_failed(job, status_data)

        mock_error.assert_called_once_with(_JOB_ID, "Azure transcription failed")
        mock_dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_default_message_when_absent(self, service):
        job = _make_pending_job()
        status_data = {}

        with (
            patch.object(service, "_record_error") as mock_error,
            patch.object(service, "_dispatch_failure", new_callable=AsyncMock),
        ):
            await service._handle_failed(job, status_data)

        args = mock_error.call_args[0]
        assert "failed" in args[1].lower()

    @pytest.mark.asyncio
    async def test_skips_webhook_when_no_callback_url(self, service):
        from transcribe_api.stt.polling_service import _PendingJob
        from transcribe_api.domain.models_recording import BatchJobStatus

        job = _PendingJob(
            id=_JOB_ID,
            batch_job_url=_JOB_URL,
            batch_job_status=BatchJobStatus.RUNNING,
            callback_url=None,
            caller_id=_CALLER_ID,
            metadata_={},
            webhook_secret=_WEBHOOK_SECRET,
        )

        with (
            patch.object(service, "_record_error"),
            patch(
                "transcribe_api.stt.polling_service.dispatch",
                new_callable=AsyncMock,
            ) as mock_dispatch,
        ):
            await service._handle_failed(job, {})

        mock_dispatch.assert_not_awaited()
