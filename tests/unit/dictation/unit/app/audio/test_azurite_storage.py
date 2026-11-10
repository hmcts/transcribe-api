"""Dictation storage and mock Speech in PR previews (Azurite, SPEECH_BACKEND=mock)."""

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager
from transcribe_api.stt.utils import generate_blob_download_url, generate_blob_upload_url

# Azurite's published development account key, as in values.preview.template.yaml.
AZURITE = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"  # gitleaks:allow
    "BlobEndpoint=http://pr-1-azurite:10000/devstoreaccount1;"
)
BLOB = "user-uploads/0d1e2f/3a4b5c6d-0000-4000-8000-000000000000.mp3"


def _settings(connection_string):
    return MagicMock(
        AZURE_STORAGE_ACCOUNT_NAME="preview-azurite",
        AZURE_STORAGE_CONTAINER_NAME="audio",
        AZURE_STORAGE_CONNECTION_STRING=connection_string,
    )


class _ServiceClient:
    def __init__(self):
        self.blob_client = MagicMock()
        self.blob_client.upload_blob = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get_blob_client(self, container, blob):
        return self.blob_client


class TestBlobManagerWithConnectionString:
    @pytest.mark.asyncio
    async def test_uses_the_connection_string_and_no_managed_identity(self):
        service = _ServiceClient()
        with (
            patch("transcribe_api.runtime.blob_dictation.get_settings", return_value=_settings(AZURITE)),
            patch("transcribe_api.runtime.blob_dictation.DefaultAzureCredential") as credential_class,
            patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient") as client_class,
        ):
            client_class.from_connection_string.return_value = service
            async with AsyncAzureBlobManager() as manager:
                ok = await manager.create_blob_from_bytes(b"audio", BLOB)

        assert ok is True
        client_class.from_connection_string.assert_called_once_with(AZURITE)
        client_class.assert_not_called()
        credential_class.assert_not_called()
        service.blob_client.upload_blob.assert_awaited_once_with(b"audio", overwrite=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("connection_string", [None, ""])
    async def test_without_it_uses_managed_identity_on_the_account_url(self, connection_string):
        service = _ServiceClient()
        credential = MagicMock(close=AsyncMock())
        with (
            patch("transcribe_api.runtime.blob_dictation.get_settings", return_value=_settings(connection_string)),
            patch("transcribe_api.runtime.blob_dictation.DefaultAzureCredential", return_value=credential),
            patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient", return_value=service) as client_class,
        ):
            async with AsyncAzureBlobManager() as manager:
                ok = await manager.create_blob_from_bytes(b"audio", BLOB)

        assert ok is True
        client_class.assert_called_once_with(
            account_url="https://preview-azurite.blob.core.windows.net", credential=credential
        )
        client_class.from_connection_string.assert_not_called()
        credential.close.assert_awaited_once()


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


@pytest.mark.asyncio
class TestSasUrlsWithConnectionString:
    @pytest.fixture(autouse=True)
    def no_managed_identity(self):
        with (
            patch("transcribe_api.stt.utils.get_settings", return_value=_settings(AZURITE)),
            patch("azure.identity.aio.DefaultAzureCredential") as credential_class,
            patch("azure.storage.blob.aio.BlobServiceClient") as service_class,
        ):
            yield
        credential_class.assert_not_called()
        service_class.assert_not_called()

    async def test_upload_url_is_account_key_signed_on_the_azurite_endpoint(self):
        url = await generate_blob_upload_url("audio", BLOB, expiry_hours=2)

        assert url.startswith(f"http://pr-1-azurite:10000/devstoreaccount1/audio/{BLOB}?")
        query = _query(url)
        assert set(query["sp"]) == {"c", "w"}
        assert query["sr"] == "b"
        assert query["sig"]
        assert "skoid" not in query  # not a user delegation SAS

    async def test_download_url_is_read_only_and_names_the_file(self):
        url = await generate_blob_download_url("audio", BLOB, expiry_hours=1, download_filename="Hearing.docx")

        assert url.startswith(f"http://pr-1-azurite:10000/devstoreaccount1/audio/{BLOB}?")
        query = _query(url)
        assert query["sp"] == "r"
        assert query["rscd"] == 'attachment; filename="Hearing.docx"'
        assert "skoid" not in query

    async def test_download_url_without_a_filename_sets_no_disposition(self):
        url = await generate_blob_download_url("audio", BLOB, expiry_hours=1)

        assert "rscd" not in _query(url)


@pytest.mark.asyncio
class TestMockSpeech:
    async def test_mock_backend_returns_a_transcript_without_calling_azure(self, tmp_path):
        from transcribe_api.stt.fast_client import transcribe_audio_with_azure

        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"audio")
        settings = MagicMock(SPEECH_BACKEND="mock", AZURE_SPEECH_ENDPOINT="")
        with (
            patch("transcribe_api.stt.fast_client.get_settings", return_value=settings),
            patch("httpx.AsyncClient") as http_client,
        ):
            entries = await transcribe_audio_with_azure(audio)

        http_client.assert_not_called()
        assert [e.speaker for e in entries] == ["1", "2"]
        assert all(e.text for e in entries)

    async def test_uploaded_audio_is_downloaded_and_gets_the_mock_transcript(self, tmp_path):
        from transcribe_api.stt import fast_client

        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"audio")
        settings = MagicMock(SPEECH_BACKEND="mock", AZURE_SPEECH_ENDPOINT="")
        with (
            patch.object(fast_client, "get_settings", return_value=settings),
            patch.object(fast_client, "download_audio_blob_with_retry", AsyncMock(return_value=audio)) as download,
        ):
            first = await fast_client.transcribe_audio(BLOB)
            first[0].text = "changed"
            second = await fast_client.transcribe_audio(BLOB)

        download.assert_awaited_with(BLOB)
        assert second[0].text != "changed"
        assert not audio.exists()  # the downloaded copy is cleaned up


def test_dictation_settings_read_the_preview_variables(monkeypatch):
    from transcribe_api.runtime.settings_dictation import ProductionSettings

    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", AZURITE)
    monkeypatch.setenv("SPEECH_BACKEND", "mock")
    settings = ProductionSettings(ENVIRONMENT="preview")

    assert settings.AZURE_STORAGE_CONNECTION_STRING == AZURITE
    assert settings.SPEECH_BACKEND == "mock"


def test_dictation_settings_default_to_managed_identity_and_azure_speech(monkeypatch):
    from transcribe_api.runtime.settings_dictation import ProductionSettings

    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("SPEECH_BACKEND", raising=False)
    settings = ProductionSettings(ENVIRONMENT="aat")

    assert settings.AZURE_STORAGE_CONNECTION_STRING is None
    assert settings.SPEECH_BACKEND == "azure"
