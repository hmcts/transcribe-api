"""Test Azure Storage utility functions for SAS URL generation with Managed Identity."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest
from azure.storage.blob import BlobSasPermissions

from transcribe_api.stt.utils import (
    convert_input_dialogue_entries_to_dialogue_entries,
    extract_transcription_id_from_blob_path,
    generate_blob_download_url,
    generate_blob_upload_url,
    get_file_blob_path,
    get_url_for_transcription,
    is_rate_limit_error,
)
from transcribe_api.domain.models_dictation import DialogueEntry


@pytest.mark.asyncio
class TestGenerateBlobUploadUrl:
    """Test cases for generate_blob_upload_url with Managed Identity."""

    @pytest.fixture
    def mock_settings(self):
        """Mock settings for testing."""
        with patch("transcribe_api.stt.utils.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.AZURE_STORAGE_ACCOUNT_NAME = "teststorage"
            mock_get_settings.return_value = mock_settings
            yield mock_settings

    @pytest.fixture
    def mock_credential(self):
        """Mock DefaultAzureCredential."""
        with patch("azure.identity.aio.DefaultAzureCredential") as mock_cred_class:
            mock_credential = AsyncMock()
            mock_credential.close = AsyncMock()
            mock_cred_class.return_value = mock_credential
            yield mock_credential

    @pytest.fixture
    def mock_blob_service_client(self):
        """Mock BlobServiceClient with user delegation key."""
        with patch("azure.storage.blob.aio.BlobServiceClient") as mock_client_class:
            # Mock the async context manager
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock user delegation key
            mock_delegation_key = MagicMock()
            mock_delegation_key.signed_oid = "test-oid"
            mock_delegation_key.signed_tid = "test-tid"
            mock_delegation_key.signed_start = datetime.now(UTC)
            mock_delegation_key.signed_expiry = datetime.now(UTC) + timedelta(hours=1)
            mock_delegation_key.signed_service = "b"
            mock_delegation_key.signed_version = "2021-06-08"
            mock_delegation_key.value = "mock_key_value"

            mock_client.get_user_delegation_key = AsyncMock(return_value=mock_delegation_key)

            yield mock_client

    @pytest.fixture
    def mock_generate_blob_sas(self):
        """Mock generate_blob_sas function."""
        with patch("transcribe_api.stt.utils.generate_blob_sas") as mock_sas:
            mock_sas.return_value = "mock_sas_token"
            yield mock_sas

    async def test_generates_url_with_user_delegation_key(
        self, mock_settings, mock_credential, mock_blob_service_client, mock_generate_blob_sas
    ):
        """Test that upload URL is generated using User Delegation Key."""
        container_name = "test-container"
        blob_name = "test-blob.wav"
        expiry_hours = 24

        url = await generate_blob_upload_url(container_name, blob_name, expiry_hours)

        # Verify get_user_delegation_key was called
        assert mock_blob_service_client.get_user_delegation_key.called

        # Verify generate_blob_sas was called with user_delegation_key
        sas_call_kwargs = mock_generate_blob_sas.call_args[1]
        assert "user_delegation_key" in sas_call_kwargs
        assert sas_call_kwargs["account_name"] == "teststorage"
        assert sas_call_kwargs["container_name"] == container_name
        assert sas_call_kwargs["blob_name"] == blob_name

        # Verify URL format
        expected_url = f"https://teststorage.blob.core.windows.net/{container_name}/{blob_name}?mock_sas_token"
        assert url == expected_url

    async def test_sets_correct_permissions_for_upload(
        self, mock_settings, mock_credential, mock_blob_service_client, mock_generate_blob_sas
    ):
        """Test that upload URL has write and create permissions."""
        await generate_blob_upload_url("container", "blob.wav", 1)

        sas_call_kwargs = mock_generate_blob_sas.call_args[1]
        permissions = sas_call_kwargs["permission"]

        assert isinstance(permissions, BlobSasPermissions)
        assert permissions.write is True
        assert permissions.create is True
        assert permissions.read is False  # Should not have read permission

    async def test_sets_correct_expiry_time(
        self, mock_settings, mock_credential, mock_blob_service_client, mock_generate_blob_sas
    ):
        """Test that expiry time is set correctly."""
        expiry_hours = 48

        await generate_blob_upload_url("container", "blob.wav", expiry_hours)

        sas_call_kwargs = mock_generate_blob_sas.call_args[1]
        expiry_time = sas_call_kwargs["expiry"]

        # Verify expiry is approximately 48 hours from now (allow 1 minute tolerance)
        now = datetime.now(UTC)
        expected_expiry = now + timedelta(hours=expiry_hours)
        time_diff = abs((expiry_time - expected_expiry).total_seconds())
        assert time_diff < 60  # Within 1 minute

    async def test_closes_credential_after_use(
        self, mock_settings, mock_credential, mock_blob_service_client, mock_generate_blob_sas
    ):
        """Test that DefaultAzureCredential is properly closed after use."""
        await generate_blob_upload_url("container", "blob.wav", 1)

        # Verify credential.close() was called
        mock_credential.close.assert_called_once()

    async def test_closes_credential_on_exception(self, mock_settings, mock_credential, mock_blob_service_client):
        """Test that credential is closed even if an exception occurs."""
        # Make get_user_delegation_key raise an exception
        mock_blob_service_client.get_user_delegation_key.side_effect = Exception("Test error")

        with pytest.raises(Exception, match="Test error"):
            await generate_blob_upload_url("container", "blob.wav", 1)

        # Verify credential.close() was still called
        mock_credential.close.assert_called_once()

    async def test_uses_default_expiry_of_1_hour(
        self, mock_settings, mock_credential, mock_blob_service_client, mock_generate_blob_sas
    ):
        """Test that default expiry is 1 hour when not specified."""
        await generate_blob_upload_url("container", "blob.wav")  # No expiry_hours argument

        sas_call_kwargs = mock_generate_blob_sas.call_args[1]
        expiry_time = sas_call_kwargs["expiry"]

        # Verify expiry is approximately 1 hour from now
        now = datetime.now(UTC)
        expected_expiry = now + timedelta(hours=1)
        time_diff = abs((expiry_time - expected_expiry).total_seconds())
        assert time_diff < 60  # Within 1 minute


@pytest.mark.asyncio
class TestGenerateBlobDownloadUrl:
    """Test cases for generate_blob_download_url with Managed Identity."""

    @pytest.fixture
    def mock_settings(self):
        """Mock settings for testing."""
        with patch("transcribe_api.stt.utils.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.AZURE_STORAGE_ACCOUNT_NAME = "teststorage"
            mock_get_settings.return_value = mock_settings
            yield mock_settings

    @pytest.fixture
    def mock_credential(self):
        """Mock DefaultAzureCredential."""
        with patch("azure.identity.aio.DefaultAzureCredential") as mock_cred_class:
            mock_credential = AsyncMock()
            mock_credential.close = AsyncMock()
            mock_cred_class.return_value = mock_credential
            yield mock_credential

    @pytest.fixture
    def mock_blob_service_client(self):
        """Mock BlobServiceClient with user delegation key."""
        with patch("azure.storage.blob.aio.BlobServiceClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            mock_delegation_key = MagicMock()
            mock_delegation_key.signed_oid = "test-oid"
            mock_delegation_key.signed_tid = "test-tid"
            mock_delegation_key.signed_start = datetime.now(UTC)
            mock_delegation_key.signed_expiry = datetime.now(UTC) + timedelta(hours=1)
            mock_delegation_key.signed_service = "b"
            mock_delegation_key.signed_version = "2021-06-08"
            mock_delegation_key.value = "mock_key_value"

            mock_client.get_user_delegation_key = AsyncMock(return_value=mock_delegation_key)

            yield mock_client

    @pytest.fixture
    def mock_generate_blob_sas(self):
        """Mock generate_blob_sas function."""
        with patch("transcribe_api.stt.utils.generate_blob_sas") as mock_sas:
            mock_sas.return_value = "mock_download_sas_token"
            yield mock_sas

    async def test_generates_url_with_user_delegation_key(
        self, mock_settings, mock_credential, mock_blob_service_client, mock_generate_blob_sas
    ):
        """Test that download URL is generated using User Delegation Key."""
        container_name = "test-container"
        blob_name = "test-blob.wav"
        expiry_hours = 2

        url = await generate_blob_download_url(container_name, blob_name, expiry_hours)

        # Verify get_user_delegation_key was called
        assert mock_blob_service_client.get_user_delegation_key.called

        # Verify generate_blob_sas was called with user_delegation_key
        sas_call_kwargs = mock_generate_blob_sas.call_args[1]
        assert "user_delegation_key" in sas_call_kwargs

        # Verify URL format
        expected_url = f"https://teststorage.blob.core.windows.net/{container_name}/{blob_name}?mock_download_sas_token"
        assert url == expected_url

    async def test_sets_correct_permissions_for_download(
        self, mock_settings, mock_credential, mock_blob_service_client, mock_generate_blob_sas
    ):
        """Test that download URL has read permission only."""
        await generate_blob_download_url("container", "blob.wav", 1)

        sas_call_kwargs = mock_generate_blob_sas.call_args[1]
        permissions = sas_call_kwargs["permission"]

        assert isinstance(permissions, BlobSasPermissions)
        assert permissions.read is True
        assert permissions.write is False  # Should not have write permission
        assert permissions.create is False  # Should not have create permission

    async def test_closes_credential_after_use(
        self, mock_settings, mock_credential, mock_blob_service_client, mock_generate_blob_sas
    ):
        """Test that DefaultAzureCredential is properly closed after use."""
        await generate_blob_download_url("container", "blob.wav", 1)

        # Verify credential.close() was called
        mock_credential.close.assert_called_once()

    async def test_requires_explicit_expiry_hours(
        self, mock_settings, mock_credential, mock_blob_service_client, mock_generate_blob_sas
    ):
        """expiry_hours has no default — callers must pass an explicit TTL (Scenario 4, ST-J6)."""
        import inspect
        sig = inspect.signature(generate_blob_download_url)
        assert sig.parameters["expiry_hours"].default is inspect.Parameter.empty


class TestIsRateLimitError:
    def test_returns_true_for_429_status_error(self):
        response = MagicMock()
        response.status_code = 429
        exc = httpx.HTTPStatusError("Too Many Requests", request=MagicMock(), response=response)
        assert is_rate_limit_error(exc) is True

    def test_returns_false_for_non_429_status(self):
        response = MagicMock()
        response.status_code = 500
        exc = httpx.HTTPStatusError("Server Error", request=MagicMock(), response=response)
        assert is_rate_limit_error(exc) is False

    def test_returns_false_for_non_http_exception(self):
        assert is_rate_limit_error(ValueError("not http")) is False

    def test_returns_false_for_generic_exception(self):
        assert is_rate_limit_error(Exception("generic")) is False


OID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


class TestGetFileBlobPath:
    def test_generates_oid_based_path(self):
        result = get_file_blob_path(OID, "audio.wav")
        assert result == f"user-uploads/{OID}/audio.wav"

    def test_includes_oid_and_filename(self):
        result = get_file_blob_path(OID, "my_file.mp3")
        assert OID in result
        assert "my_file.mp3" in result

    def test_starts_with_user_uploads(self):
        result = get_file_blob_path(OID, "f.wav")
        assert result.startswith("user-uploads/")


class TestExtractTranscriptionIdFromBlobPath:
    def test_valid_uuid_filename_returned(self):
        uuid_str = "550e8400-e29b-41d4-a716-446655440000"
        blob_path = f"user-uploads/{OID}/{uuid_str}.mp4"
        result = extract_transcription_id_from_blob_path(blob_path, OID)
        assert result == uuid_str

    def test_non_uuid_filename_returns_none(self):
        blob_path = f"user-uploads/{OID}/recording.wav"
        result = extract_transcription_id_from_blob_path(blob_path, OID)
        assert result is None

    def test_random_word_returns_none(self):
        result = extract_transcription_id_from_blob_path(f"user-uploads/{OID}/audio.mp3", OID)
        assert result is None


class TestConvertInputDialogueEntries:
    def test_converts_single_entry(self):
        entries = [{"speaker": "Guest-1", "text": "Hello", "offsetMilliseconds": 1000, "durationMilliseconds": 500}]
        result = convert_input_dialogue_entries_to_dialogue_entries(entries)
        assert len(result) == 1
        assert isinstance(result[0], DialogueEntry)
        assert result[0].speaker == "Guest-1"
        assert result[0].text == "Hello"
        assert result[0].start_time == 1.0
        assert result[0].end_time == 1.5

    def test_converts_multiple_entries(self):
        entries = [
            {"speaker": "Guest-1", "text": "A", "offsetMilliseconds": 0, "durationMilliseconds": 1000},
            {"speaker": "Guest-2", "text": "B", "offsetMilliseconds": 2000, "durationMilliseconds": 500},
        ]
        result = convert_input_dialogue_entries_to_dialogue_entries(entries)
        assert len(result) == 2
        assert result[1].start_time == 2.0
        assert result[1].end_time == 2.5

    def test_empty_list_returns_empty(self):
        assert convert_input_dialogue_entries_to_dialogue_entries([]) == []

    def test_speaker_coerced_to_string(self):
        entries = [{"speaker": 0, "text": "Hi", "offsetMilliseconds": 0, "durationMilliseconds": 100}]
        result = convert_input_dialogue_entries_to_dialogue_entries(entries)
        assert result[0].speaker == "0"


class TestGetUrlForTranscription:
    def test_builds_url_from_settings(self):
        with patch("transcribe_api.stt.utils.get_settings") as mock_get:
            settings = MagicMock()
            settings.APP_URL = "https://app.example.com"
            mock_get.return_value = settings
            uid = UUID("550e8400-e29b-41d4-a716-446655440000")
            result = get_url_for_transcription(uid)
        assert result == f"https://app.example.com/?id={uid}"

    def test_upgrades_http_to_https(self):
        with patch("transcribe_api.stt.utils.get_settings") as mock_get:
            settings = MagicMock()
            settings.APP_URL = "http://app.example.com"
            mock_get.return_value = settings
            uid = UUID("550e8400-e29b-41d4-a716-446655440000")
            result = get_url_for_transcription(uid)
        assert result.startswith("https://")
        assert "app.example.com" in result

    def test_already_https_not_double_wrapped(self):
        with patch("transcribe_api.stt.utils.get_settings") as mock_get:
            settings = MagicMock()
            settings.APP_URL = "https://my.site.com"
            mock_get.return_value = settings
            uid = UUID("550e8400-e29b-41d4-a716-446655440000")
            result = get_url_for_transcription(uid)
        assert result.count("https://") == 1


@pytest.mark.asyncio
class TestCleanupFiles:
    async def test_deletes_existing_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = Path(f.name)

        assert tmp_path.exists()
        from transcribe_api.stt.utils import cleanup_files
        await cleanup_files(tmp_path)
        assert not tmp_path.exists()

    async def test_none_path_does_nothing(self):
        from transcribe_api.stt.utils import cleanup_files
        await cleanup_files(None)  # Should not raise

    async def test_nonexistent_path_does_nothing(self):
        from transcribe_api.stt.utils import cleanup_files
        path = Path("/tmp/this_file_definitely_does_not_exist_12345.wav")  # noqa: S108
        await cleanup_files(path)  # Should not raise


class TestGenerateBlobDownloadUrlWithFilename:
    """Tests line 183: content_disposition set when download_filename is provided."""

    @pytest.mark.asyncio
    async def test_sets_content_disposition_when_download_filename_given(self):
        from transcribe_api.stt.utils import generate_blob_download_url

        with patch("transcribe_api.stt.utils.get_settings") as mock_get:
            settings = MagicMock()
            settings.AZURE_STORAGE_ACCOUNT_NAME = "teststorage"
            mock_get.return_value = settings

            with patch("azure.identity.aio.DefaultAzureCredential") as mock_cred_class:
                mock_cred = MagicMock()
                mock_cred.close = AsyncMock()
                mock_cred_class.return_value = mock_cred

                with patch("azure.storage.blob.aio.BlobServiceClient") as mock_client_class:
                    mock_client = AsyncMock()
                    mock_client_class.return_value.__aenter__.return_value = mock_client
                    mock_key = MagicMock()
                    mock_client.get_user_delegation_key = AsyncMock(return_value=mock_key)

                    with patch("transcribe_api.stt.utils.generate_blob_sas", return_value="token") as mock_sas:
                        await generate_blob_download_url(
                            "container", "blob.wav", expiry_hours=1, download_filename="MyFile.wav"
                        )

                    kwargs = mock_sas.call_args[1]
                    assert "content_disposition" in kwargs
                    assert "MyFile.wav" in kwargs["content_disposition"]


class TestConvertToMp3:
    def test_raises_file_not_found_for_missing_input(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        with pytest.raises(FileNotFoundError):
            convert_to_mp3(tmp_path / "nonexistent.wav")

    def test_raises_value_error_for_invalid_bitrate(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "audio.wav"
        f.write_bytes(b"fake")
        with pytest.raises(ValueError, match="Invalid bitrate"):
            convert_to_mp3(f, bitrate="invalid")

    def test_raises_value_error_for_bitrate_without_k_suffix(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "audio.wav"
        f.write_bytes(b"fake")
        with pytest.raises(ValueError, match="Invalid bitrate"):
            convert_to_mp3(f, bitrate="192m")

    def test_raises_value_error_for_vbr_above_9(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "audio.wav"
        f.write_bytes(b"fake")
        with pytest.raises(ValueError, match="Invalid VBR"):
            convert_to_mp3(f, vbr=10)

    def test_raises_value_error_for_vbr_below_0(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "audio.wav"
        f.write_bytes(b"fake")
        with pytest.raises(ValueError, match="Invalid VBR"):
            convert_to_mp3(f, vbr=-1)

    def test_happy_path_non_mp3_returns_output_file(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "audio.wav"
        f.write_bytes(b"fake")
        output = str(tmp_path / "out.mp3")
        with patch("transcribe_api.stt.utils.ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.probe.return_value = {"streams": [{"codec_type": "audio"}]}
            mock_ffmpeg.input.return_value = MagicMock()
            mock_ffmpeg.output.return_value = MagicMock()
            mock_ffmpeg.run.return_value = None
            result = convert_to_mp3(f, output_file=output)
        assert result == output

    def test_auto_generates_output_filename_when_not_provided(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "recording.wav"
        f.write_bytes(b"fake")
        with patch("transcribe_api.stt.utils.ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.probe.return_value = {"streams": [{"codec_type": "audio"}]}
            mock_ffmpeg.input.return_value = MagicMock()
            mock_ffmpeg.output.return_value = MagicMock()
            mock_ffmpeg.run.return_value = None
            result = convert_to_mp3(f)
        assert "recording_converted" in result
        assert result.endswith(".mp3")

    def test_raises_runtime_error_when_no_audio_streams(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "video.mp4"
        f.write_bytes(b"fake")
        with patch("transcribe_api.stt.utils.ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.probe.return_value = {"streams": [{"codec_type": "video"}]}
            with pytest.raises(RuntimeError, match="No audio stream"):
                convert_to_mp3(f)

    def test_mp3_input_uses_temp_file_and_moves_it(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "audio.mp3"
        f.write_bytes(b"fake mp3")
        output = str(tmp_path / "out.mp3")
        with patch("transcribe_api.stt.utils.ffmpeg") as mock_ffmpeg, patch("transcribe_api.stt.utils.shutil.move") as mock_move:
            mock_ffmpeg.probe.return_value = {"streams": [{"codec_type": "audio"}]}
            mock_ffmpeg.input.return_value = MagicMock()
            mock_ffmpeg.output.return_value = MagicMock()
            mock_ffmpeg.run.return_value = None
            result = convert_to_mp3(f, output_file=output)
        assert result == output
        mock_move.assert_called_once()

    def test_vbr_passed_as_qscale_a(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "audio.wav"
        f.write_bytes(b"fake")
        with patch("transcribe_api.stt.utils.ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.probe.return_value = {"streams": [{"codec_type": "audio"}]}
            mock_ffmpeg.input.return_value = MagicMock()
            mock_ffmpeg.output.return_value = MagicMock()
            mock_ffmpeg.run.return_value = None
            convert_to_mp3(f, vbr=5)
        kwargs = mock_ffmpeg.output.call_args[1]
        assert kwargs.get("qscale:a") == 5

    def test_exception_during_run_reraises(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "audio.wav"
        f.write_bytes(b"fake")
        with patch("transcribe_api.stt.utils.ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.probe.return_value = {"streams": [{"codec_type": "audio"}]}
            mock_ffmpeg.input.return_value = MagicMock()
            mock_ffmpeg.output.return_value = MagicMock()
            mock_ffmpeg.run.side_effect = RuntimeError("ffmpeg crashed")
            with pytest.raises(RuntimeError, match="ffmpeg crashed"):
                convert_to_mp3(f)

    def test_exception_with_mp3_input_cleans_up_temp_file(self, tmp_path):
        from transcribe_api.stt.utils import convert_to_mp3
        f = tmp_path / "audio.mp3"
        f.write_bytes(b"fake mp3")
        output = str(tmp_path / "out.mp3")
        with patch("transcribe_api.stt.utils.ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.probe.return_value = {"streams": [{"codec_type": "audio"}]}
            mock_ffmpeg.input.return_value = MagicMock()
            mock_ffmpeg.output.return_value = MagicMock()
            mock_ffmpeg.run.side_effect = RuntimeError("ffmpeg crashed")
            with pytest.raises(RuntimeError):
                convert_to_mp3(f, output_file=output)


class TestGetAudioDuration:
    def test_returns_duration_on_success(self, tmp_path):
        from transcribe_api.stt.utils import get_audio_duration
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "123.45\n"
        with patch("transcribe_api.stt.utils.subprocess.run", return_value=mock_result):
            duration = get_audio_duration(tmp_path / "audio.wav")
        assert duration == pytest.approx(123.45)

    def test_raises_value_error_on_nonzero_returncode(self, tmp_path):
        from transcribe_api.stt.utils import get_audio_duration
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error output"
        with patch("transcribe_api.stt.utils.subprocess.run", return_value=mock_result), pytest.raises(ValueError, match="Failed to get duration"):
            get_audio_duration(tmp_path / "audio.wav")

    def test_reraises_on_subprocess_exception(self, tmp_path):
        from transcribe_api.stt.utils import get_audio_duration
        with patch("transcribe_api.stt.utils.subprocess.run", side_effect=OSError("ffprobe not found")), pytest.raises(OSError, match="ffprobe not found"):
            get_audio_duration(tmp_path / "audio.wav")

    def test_reraises_on_float_parse_error(self, tmp_path):
        from transcribe_api.stt.utils import get_audio_duration
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not-a-number"
        with patch("transcribe_api.stt.utils.subprocess.run", return_value=mock_result), pytest.raises(ValueError, match="not-a-number"):
            get_audio_duration(tmp_path / "audio.wav")


@pytest.mark.asyncio
class TestCleanupFilesExceptionPath:
    async def test_exception_during_unlink_is_caught(self):
        from transcribe_api.stt.utils import cleanup_files

        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = Path(f.name)

        try:
            with patch.object(Path, "unlink", side_effect=PermissionError("denied")):
                await cleanup_files(temp_path)  # should not raise
        finally:
            temp_path.unlink(missing_ok=True)
