"""Integration tests for Azure Blob Storage operations."""

import contextlib
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

# Import the actual functions we want to test
from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager


@pytest.mark.integration
@pytest.mark.asyncio
class TestAzureBlobOperations:
    """Integration tests for Azure Blob Storage operations."""

    @pytest.fixture(scope="class")
    def azure_auth_check(self):
        """Check if Azure CLI is authenticated (required for managed identity tests)."""
        # Integration tests connect to real dev storage account
        # Note: We hardcode the account name because conftest.py's autouse fixture
        # overrides AZURE_STORAGE_ACCOUNT_NAME to "teststorage" for unit tests
        import subprocess

        try:
            # Safe: Running trusted Azure CLI command to check authentication status
            subprocess.run(["az", "account", "show"], check=True, capture_output=True)  # noqa: S607
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("Azure CLI not authenticated. Run: az login")
        return "courtstransdevstor"  # Hardcoded real dev storage account

    @pytest.fixture(autouse=True)
    def setup(self, azure_auth_check):
        """Setup test configuration."""
        # Azure Storage configuration (uses managed identity via az login)
        self.account_name = azure_auth_check  # "courtstransdevstor"
        self.container_name = "application-data"
        self.test_subdirectory = "tests"  # Use subdirectory to avoid clutter

        # Store connection info for debugging if needed
        self._debug_info = {
            "account": self.account_name,
            "container": self.container_name,
            "subdirectory": self.test_subdirectory,
            "auth_method": "Managed Identity (Azure CLI)",
        }

    async def test_connection_and_container_access(self, azure_auth_check):
        """Test basic connection to Azure Storage using AsyncAzureBlobManager."""
        try:
            # Test connection by creating a manager instance (uses DefaultAzureCredential)
            async with AsyncAzureBlobManager() as manager:
                # Try a simple operation that requires authentication
                # We'll just check if we can create a manager without errors
                assert manager.container_name is not None
                assert manager.account_name is not None
        except Exception as e:
            pytest.fail(f"Failed to create AsyncAzureBlobManager with Managed Identity: {e}")

    @pytest.fixture(scope="class")
    def shared_test_blob_name(self):
        """Generate shared blob name for create/delete test pair."""
        return "test-blob-shared.txt"  # Static name for create/delete pair

    @pytest.fixture
    def temp_file(self, test_file_content):
        """Create temporary file for testing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(test_file_content)
            temp_file_path = Path(f.name)
        yield temp_file_path
        if temp_file_path.exists():
            temp_file_path.unlink()

    @pytest.fixture
    def test_file_content(self):
        """Test file content."""
        return f"Integration Test File\nContent created at: {datetime.now(tz=UTC).isoformat()}"

    async def test_create_blob(self, temp_file, shared_test_blob_name, azure_auth_check):
        """Test creating a blob and verifying it exists - does NOT delete it."""
        # Create blob path with subdirectory
        blob_path = f"{self.test_subdirectory}/{shared_test_blob_name}"

        async with AsyncAzureBlobManager() as manager:
            success = await manager.create_blob_from_file(
                file_path=temp_file, blob_name=blob_path, container_name=self.container_name
            )
            assert success, "Failed to create blob using AsyncAzureBlobManager"

            # Verify it exists using AsyncAzureBlobManager (tests production code)
            exists = await manager.blob_exists(blob_name=blob_path, container_name=self.container_name)
            assert exists, "Blob should exist after creation"

    async def test_delete_blob(self, shared_test_blob_name, azure_auth_check):
        """Test deleting a blob and verifying it's gone - assumes blob already exists."""
        # Create blob path with subdirectory
        blob_path = f"{self.test_subdirectory}/{shared_test_blob_name}"

        async with AsyncAzureBlobManager() as manager:
            # First verify the blob exists (should have been created by previous test)
            exists_before = await manager.blob_exists(blob_name=blob_path, container_name=self.container_name)
            assert exists_before, f"Blob should exist before deletion: {blob_path}"

            # Delete the blob using AsyncAzureBlobManager (tests production code)
            deleted = await manager.delete_blob(blob_name=blob_path, container_name=self.container_name)
            assert deleted, "Failed to delete blob using AsyncAzureBlobManager"

            # Verify it no longer exists
            exists_after = await manager.blob_exists(blob_name=blob_path, container_name=self.container_name)
            assert not exists_after, "Blob should not exist after deletion"


@pytest.mark.integration
@pytest.mark.asyncio
class TestAsyncAzureBlobPollingOperations:
    """Integration tests for AsyncAzureBlobManager polling-related operations."""

    @pytest.fixture(scope="class")
    def azure_auth_check(self):
        """Check if Azure CLI is authenticated (required for managed identity tests)."""
        # Integration tests connect to real dev storage account
        # Note: We hardcode the account name because conftest.py's autouse fixture
        # overrides AZURE_STORAGE_ACCOUNT_NAME to "teststorage" for unit tests
        import subprocess

        try:
            # Safe: Running trusted Azure CLI command to check authentication status
            subprocess.run(["az", "account", "show"], check=True, capture_output=True)  # noqa: S607
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("Azure CLI not authenticated. Run: az login")
        return "courtstransdevstor"  # Hardcoded real dev storage account

    @pytest.fixture(autouse=True)
    def setup(self, azure_auth_check):
        """Setup test configuration."""
        # Azure Storage configuration (uses managed identity via az login)
        self.account_name = azure_auth_check
        self.container_name = "application-data"
        self.test_subdirectory = "tests/polling-integration"  # Dedicated subdirectory for polling tests

    @pytest_asyncio.fixture
    async def async_manager(self):
        """Create AsyncAzureBlobManager instance with cleanup."""
        manager = AsyncAzureBlobManager()
        yield manager
        await manager.close()

    @pytest.fixture
    def temp_file(self):
        """Create a single temporary file for testing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(f"Test content\nCreated at: {datetime.now(tz=UTC).isoformat()}")
            temp_file_path = Path(f.name)

        yield temp_file_path

        # Cleanup temp file
        if temp_file_path.exists():
            temp_file_path.unlink()

    @pytest_asyncio.fixture
    async def test_blob_with_cleanup(self, azure_auth_check, temp_file, async_manager):
        """Create a test blob and automatically clean it up after the test."""
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S_%f")
        blob_path = f"{self.test_subdirectory}/test-blob-{timestamp}.mp4"

        # Create the blob using AsyncAzureBlobManager
        success = await async_manager.create_blob_from_file(
            file_path=temp_file, blob_name=blob_path, container_name=self.container_name
        )
        assert success, f"Failed to create test blob: {blob_path}"

        yield blob_path

        # Cleanup
        with contextlib.suppress(Exception):
            await async_manager.delete_blob(blob_name=blob_path, container_name=self.container_name)

    @pytest_asyncio.fixture
    async def multiple_test_blobs_with_cleanup(self, azure_auth_check, temp_file, async_manager):
        """Create multiple test blobs and automatically clean them up after the test."""
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S_%f")
        blob_paths = [
            f"{self.test_subdirectory}/test-file-1-{timestamp}.mp4",
            f"{self.test_subdirectory}/test-file-2-{timestamp}.webm",
            f"{self.test_subdirectory}/test-file-3-{timestamp}.wav",
        ]

        # Create the blobs using AsyncAzureBlobManager
        for blob_path in blob_paths:
            success = await async_manager.create_blob_from_file(
                file_path=temp_file, blob_name=blob_path, container_name=self.container_name
            )
            assert success, f"Failed to create test blob: {blob_path}"

        yield blob_paths

        # Cleanup all blobs
        for blob_path in blob_paths:
            with contextlib.suppress(Exception):
                await async_manager.delete_blob(blob_name=blob_path, container_name=self.container_name)

    async def test_list_blobs_with_prefix(self, async_manager, multiple_test_blobs_with_cleanup):
        """Test listing blobs with a specific prefix."""
        # List blobs with our test prefix
        prefix = f"{self.test_subdirectory}/"
        listed_blobs = await async_manager.list_blobs_in_prefix(
            prefix=prefix, container_name=self.container_name, include_metadata=True
        )

        # Verify we found our test blobs
        found_blob_names = [blob["name"] for blob in listed_blobs]
        for expected_path in multiple_test_blobs_with_cleanup:
            assert expected_path in found_blob_names, f"Expected blob {expected_path} not found in list"

    async def test_list_blobs_without_metadata(self, async_manager, test_blob_with_cleanup):
        """Test listing blobs without including metadata."""
        prefix = f"{self.test_subdirectory}/"
        listed_blobs = await async_manager.list_blobs_in_prefix(
            prefix=prefix, container_name=self.container_name, include_metadata=False
        )

        # Find our test blob
        found = False
        for blob in listed_blobs:
            if blob["name"] == test_blob_with_cleanup:
                found = True
                # Verify metadata key is not present when include_metadata=False
                assert "metadata" not in blob or blob.get("metadata") is None, (
                    "Metadata should not be included when include_metadata=False"
                )
                break

        assert found, f"Test blob {test_blob_with_cleanup} should be in the list"

    async def test_set_and_get_blob_metadata(self, async_manager, test_blob_with_cleanup):
        """Test setting metadata on a blob and retrieving it."""
        # Set metadata
        test_metadata = {
            "processed": "true",
            "processed_at": datetime.now(tz=UTC).isoformat(),
            "test_run": "integration_test",
        }

        metadata_set = await async_manager.set_blob_metadata(
            blob_name=test_blob_with_cleanup, metadata=test_metadata, container_name=self.container_name
        )
        assert metadata_set, f"Failed to set metadata on blob: {test_blob_with_cleanup}"

        # Get metadata back
        retrieved_metadata = await async_manager.get_blob_metadata(
            blob_name=test_blob_with_cleanup, container_name=self.container_name
        )

        # Verify metadata matches what we set
        assert retrieved_metadata["processed"] == "true", "Metadata 'processed' should be 'true'"
        assert "processed_at" in retrieved_metadata, "Metadata should contain 'processed_at'"
        assert retrieved_metadata["test_run"] == "integration_test", "Metadata 'test_run' should match"

    async def test_get_metadata_for_blob_without_metadata(self, async_manager, test_blob_with_cleanup):
        """Test getting metadata from a blob that has no metadata set."""
        # Get metadata from blob that has no metadata
        metadata = await async_manager.get_blob_metadata(
            blob_name=test_blob_with_cleanup, container_name=self.container_name
        )

        # Should return empty dict
        assert metadata == {} or metadata is None or len(metadata) == 0, (
            "Blob without metadata should return empty dict"
        )

    async def test_list_blobs_includes_set_metadata(self, async_manager, test_blob_with_cleanup):
        """Test that list_blobs_in_prefix includes metadata that was set on a blob."""
        # First, set metadata on the blob
        test_metadata = {"processed": "true", "test_key": "test_value"}

        await async_manager.set_blob_metadata(
            blob_name=test_blob_with_cleanup, metadata=test_metadata, container_name=self.container_name
        )

        # List blobs with metadata
        prefix = f"{self.test_subdirectory}/"
        listed_blobs = await async_manager.list_blobs_in_prefix(
            prefix=prefix, container_name=self.container_name, include_metadata=True
        )

        # Find our blob in the list
        blob_info = None
        for blob in listed_blobs:
            if blob["name"] == test_blob_with_cleanup:
                blob_info = blob
                break

        # Verify metadata is included in the list
        assert blob_info is not None, "Test blob should be in the list"
        assert blob_info["metadata"]["processed"] == "true", "Listed blob should include metadata we set"
        assert blob_info["metadata"]["test_key"] == "test_value", "Listed blob should include all metadata"

    async def test_get_metadata_nonexistent_blob(self, async_manager):
        """Test getting metadata from a blob that doesn't exist."""
        nonexistent_blob = f"{self.test_subdirectory}/nonexistent-blob-{datetime.now(tz=UTC).timestamp()}.mp4"

        metadata = await async_manager.get_blob_metadata(blob_name=nonexistent_blob, container_name=self.container_name)

        # Should return empty dict for nonexistent blob
        assert metadata == {}, "Should return empty dict for nonexistent blob"

    async def test_set_metadata_nonexistent_blob(self, async_manager):
        """Test setting metadata on a blob that doesn't exist."""
        nonexistent_blob = f"{self.test_subdirectory}/nonexistent-blob-{datetime.now(tz=UTC).timestamp()}.mp4"

        result = await async_manager.set_blob_metadata(
            blob_name=nonexistent_blob, metadata={"test": "value"}, container_name=self.container_name
        )

        # Should return False for nonexistent blob
        assert result is False, "Should return False when trying to set metadata on nonexistent blob"
