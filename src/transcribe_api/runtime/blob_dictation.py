"""Azure Storage utilities for connection management and blob operations."""

import mimetypes
from pathlib import Path

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient as AsyncBlobServiceClient

from transcribe_api.runtime.logger import logger
from transcribe_api.runtime.log_sanitization import sanitize_for_log
from transcribe_api.runtime.settings_dictation import get_settings


def _sanitize_for_log(value: object) -> str:
    """Backward-compatible wrapper for log sanitization."""
    return sanitize_for_log(value)


def _redact_blob_path(blob_path: str) -> str:
    """Replace the user segment of a user-uploads blob path with [redacted].

    Legacy paths follow user-uploads/{email}/{filename}; the email is the PII that
    must not appear in logs. An email's local part may itself contain "/", so
    everything between user-uploads/ and the last component (the filename or
    filename prefix) is redacted, not only the second segment.
    Safe to call on any blob path — non-matching paths are returned unchanged.
    """
    parts = blob_path.split("/")
    if len(parts) >= 3 and parts[0] == "user-uploads":  # noqa: PLR2004
        return _sanitize_for_log(f"user-uploads/[redacted]/{parts[-1]}")
    return _sanitize_for_log(blob_path)


# =============================================================================
# Blob Management Functions
# =============================================================================


def _if_match(etag: str | None) -> dict:
    """SDK keyword arguments making a write conditional on the blob still having ``etag``."""
    if etag is None:
        return {}
    return {"etag": etag, "match_condition": MatchConditions.IfNotModified}


class AsyncAzureBlobManager:
    """Async version of Azure Blob Storage manager.

    This class provides asynchronous methods for creating, deleting, and
    checking the existence of blobs in Azure Storage.

    Uses DefaultAzureCredential for authentication which supports:
    - Managed Identity in Azure environments
    - Azure CLI credentials in local development (az login)
    """

    def __init__(self):
        """Initialize the async blob manager with DefaultAzureCredential."""
        settings = get_settings()
        self.account_name = settings.AZURE_STORAGE_ACCOUNT_NAME
        self.container_name = settings.AZURE_STORAGE_CONTAINER_NAME
        self.account_url = f"https://{self.account_name}.blob.core.windows.net"

        # Use DefaultAzureCredential which will automatically use:
        # - Managed Identity in Azure environments (uses AZURE_CLIENT_ID env var)
        # - Azure CLI credentials in local development
        self.credential = DefaultAzureCredential()

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

    async def create_blob_from_bytes(self, content: bytes, blob_name: str, container_name: str | None = None) -> bool:
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
        blob_safe = _redact_blob_path(blob_name)

        try:
            async with AsyncBlobServiceClient(account_url=self.account_url, credential=self.credential) as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                await blob_client.upload_blob(content, overwrite=True)

            logger.info(f"Successfully created blob from bytes: {container_safe}/{blob_safe}")
        except ResourceExistsError:
            logger.warning(f"Blob already exists: {container_safe}/{blob_safe}")
            return False
        except Exception as e:
            logger.error(f"Failed to create blob {container_safe}/{blob_safe}: {_sanitize_for_log(e)}")
            return False
        else:
            return True

    async def create_blob_from_file(self, file_path: Path, blob_name: str, container_name: str | None = None) -> bool:
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
        blob_safe = _redact_blob_path(blob_name)
        file_path_safe = _sanitize_for_log(file_path)

        try:
            async with AsyncBlobServiceClient(account_url=self.account_url, credential=self.credential) as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)

                # Infer content type from file extension so Azure serves
                # the correct Content-Type header on download.  Falls back
                # to application/octet-stream for unknown extensions.
                content_type, _ = mimetypes.guess_type(str(file_path))
                content_settings = ContentSettings(content_type=content_type) if content_type else None

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
            logger.error(f"Failed to create blob {container_safe}/{blob_safe}: {_sanitize_for_log(e)}")
            return False
        else:
            return True

    async def delete_blob(
        self,
        blob_name: str,
        container_name: str | None = None,
        delete_snapshots: str = "include",
        etag: str | None = None,
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
        etag : str, optional
            Only delete if the blob still has this ETag (If-Match); False if it has changed.

        Returns
        -------
        bool
            True if successful, False otherwise.
        """
        try:
            container = container_name or self.container_name

            async with AsyncBlobServiceClient(account_url=self.account_url, credential=self.credential) as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                await blob_client.delete_blob(delete_snapshots=delete_snapshots, **_if_match(etag))

            logger.info(f"Successfully deleted blob: {container}/{_redact_blob_path(blob_name)}")
        except ResourceModifiedError:
            logger.info(f"Blob changed since it was read, not deleted: {container}/{_redact_blob_path(blob_name)}")
            return False
        except ResourceNotFoundError:
            logger.warning(f"Blob not found: {container}/{_redact_blob_path(blob_name)}")
            return False
        except Exception as e:
            logger.error(f"Failed to delete blob {container}/{_redact_blob_path(blob_name)}: {e}")
            return False
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

            async with AsyncBlobServiceClient(account_url=self.account_url, credential=self.credential) as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                return await blob_client.exists()

        except Exception as e:
            logger.error(f"Failed to check if blob exists {container}/{_redact_blob_path(blob_name)}: {e}")
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
            - etag: str (pass to set_blob_metadata/delete_blob to make them conditional)
        """
        try:
            container = container_name or self.container_name
            blobs = []

            async with AsyncBlobServiceClient(account_url=self.account_url, credential=self.credential) as blob_service:
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
                        "etag": blob.etag,
                    }
                    if include_metadata:
                        blob_info["metadata"] = blob.metadata or {}
                    blobs.append(blob_info)

            logger.info(f"Listed {len(blobs)} blobs with prefix '{_redact_blob_path(prefix)}' in container '{container}'")

        except Exception as e:
            logger.error(f"Failed to list blobs with prefix '{_redact_blob_path(prefix)}' in container '{container}': {e}")
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

            async with AsyncBlobServiceClient(account_url=self.account_url, credential=self.credential) as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                properties = await blob_client.get_blob_properties()
                return properties.metadata or {}

        except ResourceNotFoundError:
            logger.warning(f"Blob not found when getting metadata: {container}/{_redact_blob_path(blob_name)}")
            return {}
        except Exception as e:
            logger.error(f"Failed to get metadata for blob {container}/{_redact_blob_path(blob_name)}: {e}")
            return {}

    async def get_blob_properties(self, blob_name: str, container_name: str | None = None) -> dict | None:
        """Get a blob's metadata together with its ETag and last-modified time (async).

        Returns
        -------
        dict | None
            {"name", "metadata", "etag", "last_modified"}, or None if the blob is not found or on error.
        """
        try:
            container = container_name or self.container_name

            async with AsyncBlobServiceClient(account_url=self.account_url, credential=self.credential) as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                properties = await blob_client.get_blob_properties()
                return {
                    "name": blob_name,
                    "metadata": properties.metadata or {},
                    "etag": properties.etag,
                    "last_modified": properties.last_modified,
                }

        except ResourceNotFoundError:
            logger.warning(f"Blob not found when getting properties: {container}/{_redact_blob_path(blob_name)}")
            return None
        except Exception as e:
            logger.error(f"Failed to get properties for blob {container}/{_redact_blob_path(blob_name)}: {e}")
            return None

    async def set_blob_metadata(
        self, blob_name: str, metadata: dict, container_name: str | None = None, etag: str | None = None
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
        etag : str, optional
            Only write if the blob still has this ETag (If-Match), so a read-then-write
            cannot overwrite a concurrent change. False if the blob has changed.

        Returns
        -------
        bool
            True if successful, False otherwise.
        """
        try:
            container = container_name or self.container_name

            async with AsyncBlobServiceClient(account_url=self.account_url, credential=self.credential) as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
                await blob_client.set_blob_metadata(metadata=metadata, **_if_match(etag))

            logger.info(f"Successfully set metadata on blob: {container}/{_redact_blob_path(blob_name)}")

        except ResourceModifiedError:
            logger.info(f"Blob changed since it was read, metadata not set: {container}/{_redact_blob_path(blob_name)}")
            return False

        except ResourceNotFoundError:
            logger.error(f"Blob not found when setting metadata: {container}/{_redact_blob_path(blob_name)}")
            return False
        except Exception as e:
            logger.error(f"Failed to set metadata on blob {container}/{_redact_blob_path(blob_name)}: {e}")
            return False
        else:
            return True

    async def download_blob_to_file(self, blob_name: str, file_path: Path, container_name: str | None = None) -> bool:
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

            async with AsyncBlobServiceClient(account_url=self.account_url, credential=self.credential) as blob_service:
                blob_client = blob_service.get_blob_client(container=container, blob=blob_name)

                # Check if blob exists first
                if not await blob_client.exists():
                    logger.error(f"Blob not found: {container}/{_redact_blob_path(blob_name)}")
                    error_msg = "Blob not found"
                    raise FileNotFoundError(error_msg)

                # Download blob content
                download_stream = await blob_client.download_blob()
                content = await download_stream.readall()

                # Write to file
                file_path.write_bytes(content)

            logger.info(f"Successfully downloaded blob {container}/{_redact_blob_path(blob_name)} to {file_path}")

        except FileNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to download blob {container}/{_redact_blob_path(blob_name)}: {e}")
            return False
        else:
            return True
