"""Unit tests for transcribe_audio_with_azure."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

VALID_PHRASE = {
    "speaker": "Guest-1",
    "text": "Hello",
    "offsetMilliseconds": 0,
    "durationMilliseconds": 1000,
    "words": [],
}


@pytest.fixture
def mock_settings():
    with patch("transcribe_api.stt.fast_client.get_settings") as mock_get:
        settings = MagicMock()
        settings.AZURE_SPEECH_KEY = "test-key"
        settings.AZURE_SPEECH_ENDPOINT = "https://test.cognitiveservices.azure.com"
        mock_get.return_value = settings
        yield settings


@pytest.fixture
def audio_file(tmp_path):
    f = tmp_path / "test.mp3"
    f.write_bytes(b"fake audio content")
    return f


@pytest.fixture
def mock_http_response():
    """Return a factory for mock httpx responses."""
    def _make(body: dict, status_code: int = 200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = body
        return resp
    return _make


@pytest.mark.asyncio
class TestTranscribeAudioWithAzure:
    async def test_missing_speech_key_raises_500(self, audio_file):
        with patch("transcribe_api.stt.fast_client.get_settings") as mock_get:
            settings = MagicMock()
            settings.AZURE_SPEECH_KEY = ""
            settings.AZURE_SPEECH_ENDPOINT = "https://test.cognitiveservices.azure.com"
            mock_get.return_value = settings

            from transcribe_api.stt.fast_client import transcribe_audio_with_azure

            with pytest.raises(HTTPException) as exc:
                await transcribe_audio_with_azure(audio_file)

            assert exc.value.status_code == 500
            assert "AZURE_SPEECH_KEY" in exc.value.detail

    async def test_missing_endpoint_raises_500(self, audio_file):
        with patch("transcribe_api.stt.fast_client.get_settings") as mock_get:
            settings = MagicMock()
            settings.AZURE_SPEECH_KEY = "test-key"
            settings.AZURE_SPEECH_ENDPOINT = ""
            mock_get.return_value = settings

            from transcribe_api.stt.fast_client import transcribe_audio_with_azure

            with pytest.raises(HTTPException) as exc:
                await transcribe_audio_with_azure(audio_file)

            assert exc.value.status_code == 500
            assert "AZURE_SPEECH_ENDPOINT" in exc.value.detail

    async def test_url_built_from_endpoint(self, mock_settings, audio_file, mock_http_response):
        """URL posted to Azure must use AZURE_SPEECH_ENDPOINT, not a hardcoded value."""
        posted_urls = []

        async def fake_post(url, **_):
            posted_urls.append(url)
            return mock_http_response({"phrases": [VALID_PHRASE]})

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = fake_post
            mock_client_class.return_value.__aenter__.return_value = mock_client

            from transcribe_api.stt.fast_client import transcribe_audio_with_azure

            await transcribe_audio_with_azure(audio_file)

        assert len(posted_urls) == 1
        assert posted_urls[0].startswith("https://test.cognitiveservices.azure.com/")
        assert "speechtotext/transcriptions:transcribe" in posted_urls[0]

    async def test_filename_sent_matches_actual_file(self, mock_settings, audio_file, mock_http_response):
        """Audio must be submitted with the real filename so Azure decodes it correctly."""
        posted_files = []

        async def fake_post(_url, **kwargs):
            posted_files.append(kwargs.get("files", {}))
            return mock_http_response({"phrases": [VALID_PHRASE]})

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = fake_post
            mock_client_class.return_value.__aenter__.return_value = mock_client

            from transcribe_api.stt.fast_client import transcribe_audio_with_azure

            await transcribe_audio_with_azure(audio_file)

        audio_tuple = posted_files[0]["audio"]
        assert audio_tuple[0] == audio_file.name  # not hardcoded "audio.webm"

    async def test_nested_error_response_raises_422(self, mock_settings, audio_file, mock_http_response):
        """Azure nested error shape {'error': {'code': '401', 'message': '...'}} surfaces correctly."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_http_response({
                "error": {"code": "401", "message": "Access denied due to invalid subscription key"}
            }))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            from transcribe_api.stt.fast_client import transcribe_audio_with_azure

            with pytest.raises(HTTPException) as exc:
                await transcribe_audio_with_azure(audio_file)

        assert exc.value.status_code == 422
        assert "Access denied due to invalid subscription key" in exc.value.detail

    async def test_top_level_error_response_raises_422(self, mock_settings, audio_file, mock_http_response):
        """Azure top-level error shape {'code': '...', 'message': '...'} also surfaces correctly."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_http_response({
                "code": "InvalidRequest", "message": "Bad audio format"
            }))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            from transcribe_api.stt.fast_client import transcribe_audio_with_azure

            with pytest.raises(HTTPException) as exc:
                await transcribe_audio_with_azure(audio_file)

        assert exc.value.status_code == 422
        assert "Bad audio format" in exc.value.detail

    async def test_empty_phrases_raises_500(self, mock_settings, audio_file, mock_http_response):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_http_response({"phrases": []}))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            from transcribe_api.stt.fast_client import transcribe_audio_with_azure

            with pytest.raises(HTTPException) as exc:
                await transcribe_audio_with_azure(audio_file)

        assert exc.value.status_code == 500
        assert "No transcription phrases found" in exc.value.detail

    async def test_missing_phrases_key_raises_500(self, mock_settings, audio_file, mock_http_response):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_http_response({}))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            from transcribe_api.stt.fast_client import transcribe_audio_with_azure

            with pytest.raises(HTTPException) as exc:
                await transcribe_audio_with_azure(audio_file)

        assert exc.value.status_code == 500


import httpx  # noqa: E402
from tenacity import stop_after_attempt, wait_none  # noqa: E402


class TestConvertToDialogueEntries:
    def test_empty_list_returns_empty(self):
        from transcribe_api.stt.fast_client import convert_to_dialogue_entries
        assert convert_to_dialogue_entries([]) == []

    def test_single_entry_creates_dialogue_entry(self):
        from transcribe_api.stt.fast_client import convert_to_dialogue_entries
        from transcribe_api.domain.models_dictation import DialogueEntry
        data = [{"speaker_label": "Speaker 0", "transcript": "Hello", "start_time": "0.0", "end_time": "1.5"}]
        result = convert_to_dialogue_entries(data)
        assert len(result) == 1
        assert isinstance(result[0], DialogueEntry)
        assert result[0].speaker == "Speaker 0"
        assert result[0].text == "Hello"
        assert result[0].start_time == 0.0
        assert result[0].end_time == 1.5

    def test_multiple_entries(self):
        from transcribe_api.stt.fast_client import convert_to_dialogue_entries
        data = [
            {"speaker_label": "Speaker 0", "transcript": "Hello", "start_time": "0.0", "end_time": "1.0"},
            {"speaker_label": "Speaker 1", "transcript": "Hi", "start_time": "1.5", "end_time": "2.5"},
        ]
        result = convert_to_dialogue_entries(data)
        assert len(result) == 2
        assert result[0].speaker == "Speaker 0"
        assert result[1].speaker == "Speaker 1"

    def test_string_times_converted_to_float(self):
        from transcribe_api.stt.fast_client import convert_to_dialogue_entries
        data = [{"speaker_label": "S", "transcript": "t", "start_time": "0.123", "end_time": "1.456"}]
        result = convert_to_dialogue_entries(data)
        assert result[0].start_time == pytest.approx(0.123)
        assert result[0].end_time == pytest.approx(1.456)


@pytest.mark.asyncio
class TestDownloadAudioBlobWithRetry:
    @pytest.fixture(autouse=True)
    def _mock_blob(self, monkeypatch):
        self.mock_blob_manager = AsyncMock()
        self.mock_blob_manager.download_blob_to_file.return_value = True
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=self.mock_blob_manager)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("transcribe_api.stt.fast_client.AsyncAzureBlobManager", lambda: mock_ctx)

    async def test_successful_download_returns_path(self):
        from transcribe_api.stt.fast_client import download_audio_blob_with_retry
        result = await download_audio_blob_with_retry("user-uploads/user/file.mp3")
        assert isinstance(result, Path)
        assert result.suffix == ".mp3"
        if result.exists():
            result.unlink()

    async def test_uses_file_extension_from_blob_path(self):
        from transcribe_api.stt.fast_client import download_audio_blob_with_retry
        result = await download_audio_blob_with_retry("user-uploads/user/recording.wav")
        assert result.suffix == ".wav"
        if result.exists():
            result.unlink()

    async def test_download_failure_raises_runtime_error(self, monkeypatch):
        from transcribe_api.stt.fast_client import download_audio_blob_with_retry
        self.mock_blob_manager.download_blob_to_file.return_value = False
        monkeypatch.setattr("transcribe_api.stt.fast_client.cleanup_files", AsyncMock())
        fast_fn = download_audio_blob_with_retry.retry_with(stop=stop_after_attempt(1), wait=wait_none())
        with pytest.raises(Exception):  # noqa: B017, PT011  # tenacity wraps RuntimeError in RetryError
            await fast_fn("user-uploads/user/file.mp3")


@pytest.mark.asyncio
class TestTranscribeAudio:
    async def test_converts_american_to_british_english(self, monkeypatch):
        from transcribe_api.stt.fast_client import transcribe_audio
        from transcribe_api.domain.models_dictation import DialogueEntry
        entries = [DialogueEntry(speaker="S", text="color", start_time=0.0, end_time=1.0)]
        monkeypatch.setattr(
            "transcribe_api.stt.fast_client.perform_transcription_steps_with_azure_and_aws",
            AsyncMock(return_value=entries),
        )
        result = await transcribe_audio("some-blob-path")
        assert result[0].text == "colour"

    async def test_returns_all_entries(self, monkeypatch):
        from transcribe_api.stt.fast_client import transcribe_audio
        from transcribe_api.domain.models_dictation import DialogueEntry
        entries = [
            DialogueEntry(speaker="S0", text="Hello", start_time=0.0, end_time=1.0),
            DialogueEntry(speaker="S1", text="Hi", start_time=1.0, end_time=2.0),
        ]
        monkeypatch.setattr(
            "transcribe_api.stt.fast_client.perform_transcription_steps_with_azure_and_aws",
            AsyncMock(return_value=entries),
        )
        result = await transcribe_audio("some-blob-path")
        assert len(result) == 2


@pytest.mark.asyncio
class TestPerformTranscriptionStepsWithAzureAndAws:
    @pytest.fixture(autouse=True)
    def _mock_deps(self, monkeypatch):
        from transcribe_api.domain.models_dictation import DialogueEntry
        self._entries = [DialogueEntry(speaker="S", text="Hi", start_time=0.0, end_time=1.0)]
        monkeypatch.setattr(
            "transcribe_api.stt.fast_client.download_audio_blob_with_retry",
            AsyncMock(return_value=Path("/tmp/test.mp3")),  # noqa: S108
        )
        monkeypatch.setattr(
            "transcribe_api.stt.fast_client.transcribe_audio_with_azure",
            AsyncMock(return_value=self._entries),
        )
        self._mock_cleanup = AsyncMock()
        monkeypatch.setattr("transcribe_api.stt.fast_client.cleanup_files", self._mock_cleanup)

    async def test_returns_transcription_results(self):
        from transcribe_api.stt.fast_client import perform_transcription_steps_with_azure_and_aws
        result = await perform_transcription_steps_with_azure_and_aws("some-blob-path")
        assert result == self._entries

    async def test_cleans_up_temp_file_on_success(self):
        from transcribe_api.stt.fast_client import perform_transcription_steps_with_azure_and_aws
        await perform_transcription_steps_with_azure_and_aws("some-blob-path")
        self._mock_cleanup.assert_called()

    async def test_cleans_up_and_reraises_on_failure(self, monkeypatch):
        from transcribe_api.stt.fast_client import perform_transcription_steps_with_azure_and_aws
        monkeypatch.setattr(
            "transcribe_api.stt.fast_client.transcribe_audio_with_azure",
            AsyncMock(side_effect=RuntimeError("transcription failed")),
        )
        mock_cleanup = AsyncMock()
        monkeypatch.setattr("transcribe_api.stt.fast_client.cleanup_files", mock_cleanup)
        with pytest.raises(RuntimeError):
            await perform_transcription_steps_with_azure_and_aws("some-blob-path")
        mock_cleanup.assert_called()

    async def test_download_failure_cleans_up(self, monkeypatch):
        from transcribe_api.stt.fast_client import perform_transcription_steps_with_azure_and_aws
        monkeypatch.setattr(
            "transcribe_api.stt.fast_client.download_audio_blob_with_retry",
            AsyncMock(side_effect=RuntimeError("download failed")),
        )
        mock_cleanup = AsyncMock()
        monkeypatch.setattr("transcribe_api.stt.fast_client.cleanup_files", mock_cleanup)
        with pytest.raises(RuntimeError):
            await perform_transcription_steps_with_azure_and_aws("some-blob-path")
        mock_cleanup.assert_called_with(None)


class TestTranscribeAudioWith429Response:
    """Tests covering line 188: response.raise_for_status() when status_code == 429."""

    @pytest.mark.asyncio
    async def test_429_response_calls_raise_for_status(self, tmp_path):
        from transcribe_api.stt.fast_client import transcribe_audio_with_azure

        audio_file = tmp_path / "test.mp3"
        audio_file.write_bytes(b"fake audio")

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Too Many Requests", request=MagicMock(), response=mock_response
        )

        with patch("transcribe_api.stt.fast_client.get_settings") as mock_get:
            settings = MagicMock()
            settings.AZURE_SPEECH_KEY = "test-key"
            settings.AZURE_SPEECH_ENDPOINT = "https://test.cognitiveservices.azure.com"
            mock_get.return_value = settings

            with patch("httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value.__aenter__.return_value = mock_client

                fast_fn = transcribe_audio_with_azure.retry_with(
                    stop=stop_after_attempt(1), wait=wait_none()
                )
                with pytest.raises(Exception):  # noqa: B017, PT011
                    await fast_fn(audio_file)

        mock_response.raise_for_status.assert_called_once()
