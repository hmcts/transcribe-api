"""Test Azure Storage utilities for AsyncAzureBlobManager with Managed Identity."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager

# =============================================================================
# AsyncAzureBlobManager Unit Tests (Proper Async Mocking)
# =============================================================================


@pytest.mark.asyncio
class TestAsyncAzureBlobManager:
    """Test cases for AsyncAzureBlobManager class with proper async mocking."""

    @pytest.fixture
    def mock_settings(self):
        """Mock settings for testing."""
        with patch("transcribe_api.runtime.blob_dictation.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.AZURE_STORAGE_ACCOUNT_NAME = "test_account"
            mock_settings.AZURE_STORAGE_CONTAINER_NAME = "test_container"
            mock_get_settings.return_value = mock_settings
            yield mock_settings

    @pytest.fixture
    def mock_credential(self):
        """Mock DefaultAzureCredential for testing."""
        with patch("transcribe_api.runtime.blob_dictation.DefaultAzureCredential") as mock_cred_class:
            mock_credential = MagicMock()
            mock_credential.close = AsyncMock()
            mock_cred_class.return_value = mock_credential
            yield mock_credential

    @pytest.fixture
    def async_blob_manager(self, mock_settings, mock_credential):
        """Create AsyncAzureBlobManager instance for testing."""
        return AsyncAzureBlobManager()

    @pytest.fixture
    def sample_file_path(self, tmp_path):
        """Create a sample file for testing."""
        file_path = tmp_path / "test_file.txt"
        file_path.write_text("test content")
        return file_path

    @pytest.fixture
    def mock_async_context_manager(self):
        """Create a proper async context manager mock."""

        class AsyncContextManagerMock:
            def __init__(self, obj_to_return):
                self.obj = obj_to_return

            async def __aenter__(self):
                return self.obj

            async def __aexit__(self, exc_type, exc, tb):
                return None

        return AsyncContextManagerMock

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    @patch("transcribe_api.runtime.blob_dictation.logger")
    async def test_create_blob_from_file_success(
        self,
        mock_logger,
        mock_async_blob_service_client_class,
        async_blob_manager,
        sample_file_path,
        mock_async_context_manager,
    ):
        """Test successful async blob creation with proper async mocking."""
        # Setup mocks using the proper async patterns
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()

        # Configure the async context manager - now using direct constructor, not from_connection_string
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client

        # Configure async methods properly - upload_blob is awaited in the async code
        mock_blob_client.upload_blob = AsyncMock()  # This needs to be AsyncMock since it's awaited

        # Test
        result = await async_blob_manager.create_blob_from_file(sample_file_path, "test_blob.txt")

        # Assertions
        assert result is True, "create_blob_from_file should return True on success"
        mock_async_blob_service_client_class.assert_called_once_with(
            account_url="https://test_account.blob.core.windows.net", credential=async_blob_manager.credential
        )
        mock_blob_service_client.get_blob_client.assert_called_once_with(
            container="test_container", blob="test_blob.txt"
        )
        mock_blob_client.upload_blob.assert_awaited_once()
        mock_logger.info.assert_called_once_with("Successfully created blob: test_container/test_blob.txt")

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_create_blob_from_file_file_not_found(
        self, mock_async_blob_service_client_class, async_blob_manager, caplog
    ):
        """Test async blob creation when file doesn't exist."""
        # Setup mocks - constructor raises FileNotFoundError
        mock_async_blob_service_client_class.side_effect = FileNotFoundError("File not found")

        # Test
        result = await async_blob_manager.create_blob_from_file(Path("nonexistent.txt"), "test_blob.txt")

        # Assertions
        assert result is False, "create_blob_from_file should return False when file doesn't exist"
        assert "File not found: nonexistent.txt" in caplog.text, "Should log file not found error"

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_create_blob_from_file_resource_exists_error(
        self,
        mock_async_blob_service_client_class,
        async_blob_manager,
        sample_file_path,
        mock_async_context_manager,
        caplog,
    ):
        """Test async blob creation when blob already exists."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.upload_blob = AsyncMock(side_effect=ResourceExistsError("Blob already exists"))

        # Test
        result = await async_blob_manager.create_blob_from_file(sample_file_path, "test_blob.txt")

        # Assertions
        assert result is False, "create_blob_from_file should return False when blob already exists"
        assert "Blob already exists: test_container/test_blob.txt" in caplog.text, (
            "Should log blob already exists warning"
        )

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_create_blob_from_file_general_exception(
        self,
        mock_async_blob_service_client_class,
        async_blob_manager,
        sample_file_path,
        mock_async_context_manager,
        caplog,
    ):
        """Test async blob creation with general exception."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.upload_blob = AsyncMock(side_effect=Exception("General error"))

        # Test
        result = await async_blob_manager.create_blob_from_file(sample_file_path, "test_blob.txt")

        # Assertions
        assert result is False, "create_blob_from_file should return False on general exception"
        assert "Failed to create blob test_container/test_blob.txt: General error" in caplog.text, (
            "Should log general error"
        )

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    @patch("transcribe_api.runtime.blob_dictation.logger")
    async def test_delete_blob_success(
        self, mock_logger, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager
    ):
        """Test successful async blob deletion."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client

        # Configure async methods properly - delete_blob is awaited in the async code
        mock_blob_client.delete_blob = AsyncMock()  # This needs to be AsyncMock since it's awaited

        # Test
        result = await async_blob_manager.delete_blob("test_blob.txt")

        # Assertions
        assert result is True, "delete_blob should return True on success"
        mock_blob_client.delete_blob.assert_awaited_once_with(delete_snapshots="include")
        mock_logger.info.assert_called_once_with("Successfully deleted blob: test_container/test_blob.txt")

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_delete_blob_not_found(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager, caplog
    ):
        """Test async blob deletion when blob doesn't exist."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.delete_blob = AsyncMock(side_effect=ResourceNotFoundError("Blob not found"))

        # Test
        result = await async_blob_manager.delete_blob("test_blob.txt")

        # Assertions
        assert result is False, "delete_blob should return False when blob not found"
        assert "Blob not found: test_container/test_blob.txt" in caplog.text, "Should log blob not found warning"

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_delete_blob_general_exception(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager, caplog
    ):
        """Test async blob deletion with general exception."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.delete_blob = AsyncMock(side_effect=Exception("General error"))

        # Test
        result = await async_blob_manager.delete_blob("test_blob.txt")

        # Assertions
        assert result is False, "delete_blob should return False on general exception"
        assert "Failed to delete blob test_container/test_blob.txt: General error" in caplog.text, (
            "Should log general error"
        )

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_blob_exists_true(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager
    ):
        """Test async blob existence check when blob exists."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        # exists() is awaited in the async code, so it needs to be AsyncMock
        mock_blob_client.exists = AsyncMock(return_value=True)

        # Test
        result = await async_blob_manager.blob_exists("test_blob.txt")

        # Assertions
        assert result is True, "blob_exists should return True when blob exists"
        mock_blob_client.exists.assert_awaited_once()

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_blob_exists_false(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager
    ):
        """Test async blob existence check when blob doesn't exist."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        # exists() is awaited in the async code, so it needs to be AsyncMock
        mock_blob_client.exists = AsyncMock(return_value=False)

        # Test
        result = await async_blob_manager.blob_exists("test_blob.txt")

        # Assertions
        assert result is False, "blob_exists should return False when blob doesn't exist"

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_blob_exists_exception(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager, caplog
    ):
        """Test async blob existence check with exception."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.exists = AsyncMock(side_effect=Exception("General error"))

        # Test
        result = await async_blob_manager.blob_exists("test_blob.txt")

        # Assertions
        assert result is False, "blob_exists should return False on general exception"
        assert "Failed to check if blob exists test_container/test_blob.txt: General error" in caplog.text, (
            "Should log general error"
        )

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    @patch("transcribe_api.runtime.blob_dictation.logger")
    async def test_list_blobs_in_prefix_success(
        self, mock_logger, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager
    ):
        """Test successful listing of blobs with a prefix."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_container_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_container_client.return_value = mock_container_client

        # Mock blob items returned from list_blobs
        mock_blob1 = MagicMock()
        mock_blob1.name = "user-uploads/test@example.com/file1.mp4"
        mock_blob1.last_modified = "2024-01-01T12:00:00Z"
        mock_blob1.size = 1024
        mock_blob1.metadata = {"processed": "false"}

        mock_blob2 = MagicMock()
        mock_blob2.name = "user-uploads/test@example.com/file2.mp4"
        mock_blob2.last_modified = "2024-01-02T12:00:00Z"
        mock_blob2.size = 2048
        mock_blob2.metadata = None

        # list_blobs returns an async iterable
        async def mock_list_blobs(*args, **kwargs):
            for blob in [mock_blob1, mock_blob2]:
                yield blob

        mock_container_client.list_blobs = mock_list_blobs

        # Test
        result = await async_blob_manager.list_blobs_in_prefix("user-uploads/")

        # Assertions
        assert len(result) == 2, "Should return 2 blobs"
        assert result[0]["name"] == "user-uploads/test@example.com/file1.mp4"
        assert result[0]["metadata"] == {"processed": "false"}
        assert result[1]["name"] == "user-uploads/test@example.com/file2.mp4"
        assert result[1]["metadata"] == {}  # None should be converted to {}
        mock_logger.info.assert_called_once_with(
            "Listed 2 blobs with prefix 'user-uploads/' in container 'test_container'"
        )

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    @patch("transcribe_api.runtime.blob_dictation.logger")
    async def test_list_blobs_in_prefix_empty(
        self, mock_logger, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager
    ):
        """Test listing blobs when no blobs match the prefix."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_container_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_container_client.return_value = mock_container_client

        # list_blobs returns empty async iterable
        async def mock_list_blobs(*args, **kwargs):
            # Empty async generator
            if False:  # Never executes, but makes it a generator
                yield

        mock_container_client.list_blobs = mock_list_blobs

        # Test
        result = await async_blob_manager.list_blobs_in_prefix("nonexistent-prefix/")

        # Assertions
        assert len(result) == 0, "Should return empty list"
        mock_logger.info.assert_called_once_with(
            "Listed 0 blobs with prefix 'nonexistent-prefix/' in container 'test_container'"
        )

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_list_blobs_in_prefix_exception(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager, caplog
    ):
        """Test listing blobs with exception."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_container_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_container_client.return_value = mock_container_client

        # list_blobs raises exception
        async def mock_list_blobs(*args, **kwargs):
            error_msg = "List error"
            raise RuntimeError(error_msg)
            yield  # Make it a generator # noqa: RET502, RUF100

        mock_container_client.list_blobs = mock_list_blobs

        # Test
        result = await async_blob_manager.list_blobs_in_prefix("user-uploads/")

        # Assertions
        assert result == [], "Should return empty list on exception"
        assert (
            "Failed to list blobs with prefix 'user-uploads/' in container 'test_container': List error" in caplog.text
        )

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_get_blob_metadata_success(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager
    ):
        """Test successful retrieval of blob metadata."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client

        # Mock blob properties
        mock_properties = MagicMock()
        mock_properties.metadata = {"processed": "true", "processed_at": "2024-01-01T12:00:00Z"}
        mock_blob_client.get_blob_properties = AsyncMock(return_value=mock_properties)

        # Test
        result = await async_blob_manager.get_blob_metadata("user-uploads/test@example.com/file.mp4")

        # Assertions
        assert result == {"processed": "true", "processed_at": "2024-01-01T12:00:00Z"}
        mock_blob_client.get_blob_properties.assert_awaited_once()

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_get_blob_metadata_none(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager
    ):
        """Test retrieval of blob metadata when metadata is None."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client

        # Mock blob properties with None metadata
        mock_properties = MagicMock()
        mock_properties.metadata = None
        mock_blob_client.get_blob_properties = AsyncMock(return_value=mock_properties)

        # Test
        result = await async_blob_manager.get_blob_metadata("user-uploads/test@example.com/file.mp4")

        # Assertions
        assert result == {}, "Should return empty dict when metadata is None"

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_get_blob_metadata_not_found(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager, caplog
    ):
        """Test retrieval of blob metadata when blob doesn't exist."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.get_blob_properties = AsyncMock(side_effect=ResourceNotFoundError("Blob not found"))

        # Test
        result = await async_blob_manager.get_blob_metadata("nonexistent.mp4")

        # Assertions
        assert result == {}, "Should return empty dict when blob not found"
        assert "Blob not found when getting metadata: test_container/nonexistent.mp4" in caplog.text

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_get_blob_metadata_exception(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager, caplog
    ):
        """Test retrieval of blob metadata with general exception."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.get_blob_properties = AsyncMock(side_effect=Exception("General error"))

        # Test
        result = await async_blob_manager.get_blob_metadata("test.mp4")

        # Assertions
        assert result == {}, "Should return empty dict on exception"
        assert "Failed to get metadata for blob test_container/test.mp4: General error" in caplog.text

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    @patch("transcribe_api.runtime.blob_dictation.logger")
    async def test_set_blob_metadata_success(
        self, mock_logger, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager
    ):
        """Test successful setting of blob metadata."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.set_blob_metadata = AsyncMock()

        # Test
        metadata = {"processed": "true", "processed_at": "2024-01-01T12:00:00Z"}
        result = await async_blob_manager.set_blob_metadata("user-uploads/test@example.com/file.mp4", metadata)

        # Assertions
        assert result is True, "set_blob_metadata should return True on success"
        mock_blob_client.set_blob_metadata.assert_awaited_once_with(metadata=metadata)
        mock_logger.info.assert_called_once_with(
            "Successfully set metadata on blob: test_container/user-uploads/test@example.com/file.mp4"
        )

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_set_blob_metadata_not_found(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager, caplog
    ):
        """Test setting blob metadata when blob doesn't exist."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.set_blob_metadata = AsyncMock(side_effect=ResourceNotFoundError("Blob not found"))

        # Test
        result = await async_blob_manager.set_blob_metadata("nonexistent.mp4", {"key": "value"})

        # Assertions
        assert result is False, "set_blob_metadata should return False when blob not found"
        assert "Blob not found when setting metadata: test_container/nonexistent.mp4" in caplog.text

    @patch("transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient")
    async def test_set_blob_metadata_exception(
        self, mock_async_blob_service_client_class, async_blob_manager, mock_async_context_manager, caplog
    ):
        """Test setting blob metadata with general exception."""
        # Setup mocks
        mock_blob_service_client = MagicMock()
        mock_blob_client = MagicMock()
        mock_async_blob_service_client_class.return_value = mock_async_context_manager(mock_blob_service_client)
        mock_blob_service_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.set_blob_metadata = AsyncMock(side_effect=Exception("General error"))

        # Test
        result = await async_blob_manager.set_blob_metadata("test.mp4", {"key": "value"})

        # Assertions
        assert result is False, "set_blob_metadata should return False on exception"
        assert "Failed to set metadata on blob test_container/test.mp4: General error" in caplog.text
