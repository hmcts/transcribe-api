"""Azure Storage utilities for connection management and blob operations."""

import logging
import mimetypes
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient as AsyncBlobServiceClient

from transcribe_api.runtime.settings_recording import get_settings

logger = logging.getLogger(__name__)


def _sanitize_for_log(value: object) -> str:
    """Stringify and repr-wrap value to prevent log injection via control characters."""
    return repr(str(value))


# =============================================================================
# Blob Management Functions
# =============================================================================


class AsyncAzureBlobManager:
    """Async version of Azure Blob Storage manager.

    This class provides asynchronous methods for creating, deleting, and
    checking the existence of blobs in Azure Storage.

    Uses DefaultAzureCredential for authentication which supports:
    - Managed Identity in Azure environments
    - Azure CLI credentials in local development (az login)
    """

    def __init__(self):
        settings = get_settings()
        self.account_name = settings.AZURE_STORAGE_ACCOUNT_NAME
        self.container_name = settings.AZURE_STORAGE_CONTAINER_NAME
        self.account_url = f"https://{self.account_name}.blob.core.windows.net"
        self._connection_string = settings.AZURE_STORAGE_CONNECTION_STRING
        # Only create DefaultAzureCredential when no connection string is configured.
        # In deployed envs Managed Identity is used; locally a connection string avoids
        # mounting ~/.azure into the container.
        self.credential = None if self._connection_string else DefaultAzureCredential()

    def _blob_service_client(self) -> AsyncBlobServiceClient:
        if self._connection_string:
            return AsyncBlobServiceClient.from_connection_string(self._connection_string)
        return AsyncBlobServiceClient(account_url=self.account_url, credential=self.credential)

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - ensure credential is closed."""
        await self.close()

    async def close(self):
        """Close the credential and clean up resources."""
        try:
            if self.credential:
                await self.credential.close()
                logger.debug("Closed DefaultAzureCredential for Blob Storage")
        except Exception as e:
            logger.warning(f"Error closing credential: {e}")

    def build_blob_url(self, blob_name: str, container_name: str | None = None) -> str:
        """Return a plain (no-SAS) blob URL for Azure Speech Batch.

        Speech's batch backend authenticates directly against this storage
        account via its own system-assigned managed identity, under Azure's
        "trusted Azure services" mechanism (a storage network_rules
        resource-instance exception plus a Storage Blob Data Reader role
        grant on the Speech resource — see the infra repo's storage.tf).
        Microsoft's docs are explicit that a SAS token alongside
        managed-identity auth makes the request fail, so this must stay
        SAS-free — unlike generate_read_sas_url, which is for callers
        without a trusted-service grant.
        """
        container = container_name or self.container_name
        return f"{self.account_url}/{container}/{quote(blob_name, safe='/')}"

    async def create_blob_from_bytes(
        self, content: bytes, blob_name: str, container_name: str | None = None
    ) -> bool:
        """Create a blob from bytes content (async).

        Parameters
        ----------
        content : bytes
            The bytes content to upload.
        blob_name : str
            Name of the blob in Azure Storage.
        container_name : str, optional
            Container name. If None, uses the default container from settings.

        Returns
        -------
        bool
            True if successful, False otherwise.
        """
        container = container_name or self.container_name
        container_safe = _sanitize_for_log(container)
        blob_safe = _sanitize_for_log(blob_name)

        try:
            async with self._blob_service_client() as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                await blob_client.upload_blob(content, overwrite=True)

            logger.info(f"Successfully created blob from bytes: {container_safe}/{blob_safe}")
        except ResourceExistsError:
            logger.warning(f"Blob already exists: {container_safe}/{blob_safe}")
            return False
        except Exception as e:
            logger.error(
                f"Failed to create blob {container_safe}/{blob_safe}: {_sanitize_for_log(e)}"
            )
            return False
        else:
            return True

    async def create_blob_from_file(
        self, file_path: Path, blob_name: str, container_name: str | None = None
    ) -> bool:
        """Create a blob from a local file (async).

        Parameters
        ----------
        file_path : Path
            Path to the local file to upload.
        blob_name : str
            Name of the blob in Azure Storage.
        container_name : str, optional
            Container name. If None, uses the default container from settings.

        Returns
        -------
        bool
            True if successful, False otherwise.
        """
        container = container_name or self.container_name
        container_safe = _sanitize_for_log(container)
        blob_safe = _sanitize_for_log(blob_name)
        file_path_safe = _sanitize_for_log(file_path)

        try:
            async with self._blob_service_client() as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)

                # Infer content type from file extension so Azure serves
                # the correct Content-Type header on download.  Falls back
                # to application/octet-stream for unknown extensions.
                content_type, _ = mimetypes.guess_type(str(file_path))
                content_settings = (
                    ContentSettings(content_type=content_type) if content_type else None
                )

                # Upload the file
                with file_path.open("rb") as data:
                    await blob_client.upload_blob(
                        data,
                        overwrite=True,
                        content_settings=content_settings,
                    )

            logger.info(f"Successfully created blob: {container_safe}/{blob_safe}")
        except FileNotFoundError:
            logger.error(f"File not found: {file_path_safe}")
            return False
        except ResourceExistsError:
            logger.warning(f"Blob already exists: {container_safe}/{blob_safe}")
            return False
        except Exception as e:
            logger.error(
                f"Failed to create blob {container_safe}/{blob_safe}: {_sanitize_for_log(e)}"
            )
            return False
        else:
            return True

    async def delete_blob(
        self, blob_name: str, container_name: str | None = None, delete_snapshots: str = "include"
    ) -> bool:
        """Delete a blob from Azure Storage (async).

        Parameters
        ----------
        blob_name : str
            Name of the blob to delete.
        container_name : str, optional
            Container name. If None, uses the default container from settings.
        delete_snapshots : str, optional
            How to handle snapshots. Options: "include", "only", None.
            Default is "include".

        Returns
        -------
        bool
            True if the blob was deleted, False if it did not exist.

        Raises
        ------
        Exception
            On a genuine storage error (anything other than not-found), so
            callers can distinguish "already gone" from "delete failed".
        """
        try:
            container = container_name or self.container_name

            async with self._blob_service_client() as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                await blob_client.delete_blob(delete_snapshots=delete_snapshots)

            logger.info(f"Successfully deleted blob: {container}/{blob_name}")
        except ResourceNotFoundError:
            # Not found is idempotent success for callers: there is nothing
            # left to remove. Distinguished from a genuine storage error
            # (re-raised below) so a delete endpoint can 502 on the latter
            # while treating this as "already deleted".
            logger.warning(f"Blob not found: {container}/{blob_name}")
            return False
        except Exception as e:
            logger.error(f"Failed to delete blob {container}/{blob_name}: {e}")
            raise
        else:
            return True

    async def blob_exists(self, blob_name: str, container_name: str | None = None) -> bool:
        """Check if a blob exists in Azure Storage (async).

        Parameters
        ----------
        blob_name : str
            Name of the blob to check.
        container_name : str, optional
            Container name. If None, uses the default container from settings.

        Returns
        -------
        bool
            True if blob exists, False otherwise.
        """
        try:
            container = container_name or self.container_name

            async with self._blob_service_client() as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                return await blob_client.exists()

        except Exception as e:
            logger.error(f"Failed to check if blob exists {container}/{blob_name}: {e}")
            return False

    async def list_blobs_in_prefix(
        self, prefix: str, container_name: str | None = None, include_metadata: bool = True
    ) -> list[dict]:
        """List all non-deleted blobs with a given prefix (async).

        Parameters
        ----------
        prefix : str
            The prefix to filter blobs by (e.g., "user-uploads/").
        container_name : str, optional
            Container name. If None, uses the default container from settings.
        include_metadata : bool, optional
            Whether to include blob metadata in results. Default is True.

        Returns
        -------
        list[dict]
            List of dictionaries containing blob information:
            - name: str (blob name/path)
            - metadata: dict (blob metadata if include_metadata=True)
            - last_modified: datetime
            - size: int (blob size in bytes)
        """
        try:
            container = container_name or self.container_name
            blobs = []

            async with self._blob_service_client() as blob_service:
                container_client = blob_service.get_container_client(container)

                # List blobs with the given prefix
                # By default, this excludes soft-deleted blobs
                async for blob in container_client.list_blobs(
                    name_starts_with=prefix, include=["metadata"] if include_metadata else None
                ):
                    blob_info = {
                        "name": blob.name,
                        "last_modified": blob.last_modified,
                        "size": blob.size,
                    }
                    if include_metadata:
                        blob_info["metadata"] = blob.metadata or {}
                    blobs.append(blob_info)

            logger.info(
                f"Listed {len(blobs)} blobs with prefix '{prefix}' in container '{container}'"
            )

        except Exception as e:
            logger.error(
                f"Failed to list blobs with prefix '{prefix}' in container '{container}': {e}"
            )
            return []
        else:
            return blobs

    async def get_blob_metadata(self, blob_name: str, container_name: str | None = None) -> dict:
        """Get metadata for a specific blob (async).

        Parameters
        ----------
        blob_name : str
            Name of the blob.
        container_name : str, optional
            Container name. If None, uses the default container from settings.

        Returns
        -------
        dict
            Dictionary containing blob metadata. Empty dict if blob not found or error.
        """
        try:
            container = container_name or self.container_name

            async with self._blob_service_client() as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                properties = await blob_client.get_blob_properties()
                return properties.metadata or {}

        except ResourceNotFoundError:
            logger.warning(f"Blob not found when getting metadata: {container}/{blob_name}")
            return {}
        except Exception as e:
            logger.error(f"Failed to get metadata for blob {container}/{blob_name}: {e}")
            return {}

    async def set_blob_metadata(
        self, blob_name: str, metadata: dict, container_name: str | None = None
    ) -> bool:
        """Set metadata on a specific blob (async).

        Parameters
        ----------
        blob_name : str
            Name of the blob.
        metadata : dict
            Dictionary of metadata key-value pairs to set on the blob.
            Keys must be valid HTTP header names (alphanumeric + underscore).
        container_name : str, optional
            Container name. If None, uses the default container from settings.

        Returns
        -------
        bool
            True if successful, False otherwise.
        """
        try:
            container = container_name or self.container_name

            async with self._blob_service_client() as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                await blob_client.set_blob_metadata(metadata=metadata)

            logger.info(f"Successfully set metadata on blob: {container}/{blob_name}")

        except ResourceNotFoundError:
            logger.error(f"Blob not found when setting metadata: {container}/{blob_name}")
            return False
        except Exception as e:
            logger.error(f"Failed to set metadata on blob {container}/{blob_name}: {e}")
            return False
        else:
            return True

    async def get_blob_size(self, blob_name: str, container_name: str | None = None) -> int | None:
        """Return a blob's size in bytes, or None if it doesn't exist."""
        container = container_name or self.container_name
        try:
            async with self._blob_service_client() as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                properties = await blob_client.get_blob_properties()
                return properties.size
        except ResourceNotFoundError:
            return None

    async def download_blob_range(
        self, blob_name: str, start: int, length: int, container_name: str | None = None
    ) -> bytes | None:
        """Download a byte range of a blob's content (async). None if it doesn't exist.

        Used to serve HTTP Range requests (e.g. audio playback seeking)
        without pulling the whole blob into memory.
        """
        container = container_name or self.container_name
        try:
            async with self._blob_service_client() as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                download_stream = await blob_client.download_blob(offset=start, length=length)
                return await download_stream.readall()
        except ResourceNotFoundError:
            return None

    async def stream_blob_range(
        self, blob_name: str, start: int, length: int, container_name: str | None = None
    ) -> AsyncIterator[bytes]:
        """Stream a byte range of a blob's content in fixed-size chunks.

        Unlike download_blob_range(), this never buffers the whole
        requested range in memory at once — needed so serving a large
        (potentially multi-hour) recording for HTTP Range playback doesn't
        cause a memory spike proportional to the recording's length.
        Callers should confirm the blob exists (e.g. via get_blob_size)
        before starting to stream it, since once the caller has begun
        writing a streaming HTTP response, a not-found error surfacing
        mid-stream can no longer be turned into a clean 404.
        """
        container = container_name or self.container_name
        async with self._blob_service_client() as blob_service:
            blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
            download_stream = await blob_client.download_blob(offset=start, length=length)
            async for chunk in download_stream.chunks():
                yield chunk

    async def download_blob_to_file(
        self, blob_name: str, file_path: Path, container_name: str | None = None
    ) -> bool:
        """Download a blob to a local file (async).

        Parameters
        ----------
        blob_name : str
            Name/path of the blob to download.
        file_path : Path
            Local file path where the blob content should be written.
        container_name : str, optional
            Container name. If None, uses the default container from settings.

        Returns
        -------
        bool
            True if download successful, False otherwise.

        Raises
        ------
        FileNotFoundError
            If the blob doesn't exist in storage.
        """
        try:
            container = container_name or self.container_name

            async with self._blob_service_client() as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)

                # Check if blob exists first
                if not await blob_client.exists():
                    logger.error(f"Blob not found: {container}/{blob_name}")
                    error_msg = "Blob not found"
                    raise FileNotFoundError(error_msg)

                # Download blob content
                download_stream = await blob_client.download_blob()
                content = await download_stream.readall()

                # Write to file
                file_path.write_bytes(content)

            logger.info(f"Successfully downloaded blob {container}/{blob_name} to {file_path}")

        except FileNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to download blob {container}/{blob_name}: {e}")
            return False
        else:
            return True
