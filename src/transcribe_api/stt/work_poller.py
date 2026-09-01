"""
Transcription Polling Service for automatic audio file discovery and processing.

This service polls Azure Blob Storage for new audio files and automatically triggers
transcription processing without requiring frontend API calls. This avoids JWT timeout
issues on long audio recordings.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager
from transcribe_api.stt.pipeline import transcribe_and_generate_llm_output
from transcribe_api.stt.utils import extract_transcription_id_from_blob_path
from transcribe_api.runtime.db_dictation import engine
from transcribe_api.domain.models_dictation import User
from transcribe_api.runtime.logger import logger
from transcribe_api.runtime.settings_dictation import get_settings


class TranscriptionPollingService:
    """
    Global service for polling Azure Blob Storage and automatically processing audio files for all users.

    This service:
    - Polls the entire user-uploads/ prefix every 30 seconds
    - Identifies unprocessed audio files across all users
    - Triggers transcription processing
    - Marks files as processed using blob metadata
    """

    def __init__(self):
        """Initialize the global transcription polling service.

        Parameters
        ----------
        num_workers : int
            Number of parallel worker tasks to process audio files (default: 3)
        """
        self.settings = get_settings()
        self.azure_blob_manager = AsyncAzureBlobManager()
        self.polling_interval_seconds = 30
        self.user_uploads_prefix = "user-uploads/"
        self.supported_extensions = {".mp4", ".webm", ".wav", ".m4a", ".mp3", ".ogg", ".aac", ".mov"}
        self.max_retry_attempts = 2  # Allow 2 total attempts (1 retry max as requested)
        # Record startup time - only process files uploaded after this
        self.startup_time = datetime.now(UTC)
        self._is_first_poll = True

        # Queue for discovered blobs to be processed
        self.blob_queue: asyncio.Queue = asyncio.Queue()
        self.num_workers = 20
        self.worker_tasks: list[asyncio.Task] = []
        self._shutdown = False

        logger.info(
            f"Global polling service initialized with {self.num_workers} workers - "
            f"will process files for all users uploaded after {self.startup_time.isoformat()}"
        )

    def _should_skip_blob(self, blob: dict) -> bool:
        """
        Check if a blob should be skipped when looking for files to process.

        Used during polling to filter out blobs that should NOT be transcribed:
        - Already successfully processed blobs
        - Permanently failed blobs (exceeded retry limit)
        - Currently being processed blobs (in_progress status)
        - Non-audio files
        - Files not in the user-uploads prefix

        Returns True if the blob should be skipped for transcription processing.

        Parameters
        ----------
        blob : dict
            Blob information dictionary.

        Returns
        -------
        bool
            True if blob should be skipped (don't process), False if it should be processed.
        """
        blob_name = blob["name"]

        # Ensure blob is in user-uploads prefix
        if not blob_name.startswith(self.user_uploads_prefix):
            return True

        # Check file extension
        blob_path = Path(blob_name)
        if blob_path.suffix.lower() not in self.supported_extensions:
            return True

        # Check metadata for processing status
        metadata = blob.get("metadata", {})

        # Skip if already successfully processed
        if metadata.get("processed") == "true":
            return True

        # Skip if permanently failed
        if metadata.get("status") == "permanently_failed":
            logger.debug(f"Skipping permanently failed blob: {blob_name}")
            return True

        # Skip if currently being processed (prevents duplicate processing)
        if metadata.get("status") == "in_progress":
            logger.debug(f"Skipping blob already in progress: {blob_name}")
            return True

        # Don't skip if status is "reset_from_stale" or "retrying" - these should be reprocessed
        # Also don't skip new blobs with no metadata

        return False

    async def poll_for_new_audio_files(self) -> list[dict]:
        """
        Poll blob storage for new, unprocessed audio files across all users.

        Returns
        -------
        list[dict]
            List of unprocessed blob dictionaries with keys:
            - name: str (blob path)
            - metadata: dict
            - last_modified: datetime
            - size: int
        """
        try:
            # List all blobs with user-uploads prefix (all users)
            all_blobs = await self.azure_blob_manager.list_blobs_in_prefix(
                prefix=self.user_uploads_prefix, include_metadata=True
            )

            # Filter for unprocessed audio files
            unprocessed = []
            for blob in all_blobs:
                if self._should_skip_blob(blob):
                    continue

                # Check retry count to avoid infinite loops
                metadata = blob.get("metadata", {})
                retry_count = int(metadata.get("retry_count", "0"))
                if retry_count >= self.max_retry_attempts:
                    logger.warning(
                        f"Blob {blob['name']} has exceeded max retries "
                        f"({retry_count}/{self.max_retry_attempts}), marking as permanently failed"
                    )
                    await self._mark_blob_permanently_failed(blob["name"], metadata)
                    continue

                unprocessed.append(blob)

            if unprocessed:
                logger.info(f"Found {len(unprocessed)} unprocessed audio files across all users")

        except Exception as e:
            logger.error(f"Error polling for new audio files: {e}")
            return []
        else:
            return unprocessed

    def extract_user_email_from_blob_path(self, blob_path: str) -> str | None:
        """
        Extract user email from blob path.

        Expected format: user-uploads/{email}/{filename}

        Parameters
        ----------
        blob_path : str
            The full blob path.

        Returns
        -------
        str | None
            The user email if extraction is successful, None otherwise.
        """
        min_parts = 3  # Minimum parts for valid path: user-uploads/{email}/{filename}
        try:
            parts = blob_path.split("/")
            if len(parts) >= min_parts and parts[0] == "user-uploads":
                return parts[1]

        except Exception as e:
            logger.error(f"Error extracting email from blob path '{blob_path}': {e}")

        return None

    def get_or_create_user_by_email(self, email: str) -> User | None:
        """
        Look up user by email in the database.

        Note: This only looks up existing users. New users must be created
        through the authentication flow.

        Parameters
        ----------
        email : str
            The user's email address.

        Returns
        -------
        User | None
            The User object if found, None otherwise.
        """
        try:
            with Session(engine) as session:
                statement = select(User).where(User.email == email)
                user = session.exec(statement).first()

                if user:
                    logger.info(f"Found user for email: {email}")
                else:
                    logger.warning(f"No user found for email: {email}")

                return user

        except Exception as e:
            logger.error(f"Error looking up user by email '{email}': {e}")
            return None

    async def process_discovered_audio(self, blob_info: dict) -> bool:
        """
        Process a discovered audio file.

        This method:
        1. Marks blob as in_progress to prevent duplicate processing
        2. Extracts user email from blob path
        3. Looks up user in database
        4. Triggers transcription processing
        5. Marks blob as processed

        Parameters
        ----------
        blob_info : dict
            Dictionary containing blob information from poll_for_new_audio_files.

        Returns
        -------
        bool
            True if processing was successful, False otherwise.
        """
        blob_path = blob_info["name"]
        logger.info(f"Processing discovered audio file: {blob_path}")

        try:
            # CRITICAL: Mark as in_progress immediately to prevent race conditions
            # If this returns False, another worker is already processing this blob
            marked = await self._mark_blob_in_progress(blob_path)
            if not marked:
                logger.warning(f"Blob already being processed by another worker, skipping: {blob_path}")
                return False

            # Extract user email from path
            user_email = self.extract_user_email_from_blob_path(blob_path)
            if not user_email:
                error_msg = f"Could not extract user email from blob path: {blob_path}"
                logger.error(error_msg)
                await self._mark_blob_with_error(blob_path, error_msg)
                return False

            # Look up user in database
            user = self.get_or_create_user_by_email(user_email)
            if not user:
                error_msg = f"User not found for email: {user_email}"
                logger.error(error_msg)
                await self._mark_blob_with_error(blob_path, error_msg)
                return False

            # Trigger transcription processing
            transcription_id = extract_transcription_id_from_blob_path(blob_path, user_email)

            logger.info(f"Starting transcription for blob: {blob_path} (user: {user_email})")

            await transcribe_and_generate_llm_output(
                user_upload_blob_storage_file_key=blob_path,
                user_id=user.id,
                user_email=user.email,
                azure_user_id=str(user.azure_user_id),
                transcription_id=transcription_id,
            )

            # Mark blob as processed
            await self._mark_blob_as_processed_and_soft_delete(blob_path)

            logger.info(f"Successfully processed audio file: {blob_path} (user: {user_email})")

        except Exception as e:
            error_msg = f"Error processing audio file {blob_path}: {e}"
            logger.error(error_msg)
            await self._mark_blob_with_error(blob_path, str(e))
            return False
        else:
            return True

    async def _mark_blob_in_progress(self, blob_path: str) -> bool:
        """
        Mark a blob as currently being processed to prevent duplicate processing.

        This is called at the start of processing to prevent the polling loop
        from discovering and queuing the same blob multiple times if processing
        takes longer than the polling interval.

        Parameters
        ----------
        blob_path : str
            The blob path.

        Returns
        -------
        bool
            True if successfully marked, False otherwise.
        """
        try:
            # Get current metadata to preserve retry count
            current_metadata = await self.azure_blob_manager.get_blob_metadata(blob_path) or {}

            # Check if already in progress (race condition)
            if current_metadata.get("status") == "in_progress":
                logger.warning(f"Blob already marked as in_progress: {blob_path}")
                return False

            metadata = {
                "processed": "false",
                "status": "in_progress",
                "started_at": datetime.now(UTC).isoformat(),
                "retry_count": current_metadata.get("retry_count", "0"),
            }
            success = await self.azure_blob_manager.set_blob_metadata(blob_name=blob_path, metadata=metadata)
            if success:
                logger.info(f"Marked blob as in_progress: {blob_path}")
                return True
            else:
                logger.warning(f"Failed to mark blob as in_progress: {blob_path}")
                return False

        except Exception as e:
            logger.error(f"Error marking blob as in_progress {blob_path}: {e}")
            return False

    async def _mark_blob_as_processed_and_soft_delete(self, blob_path: str) -> None:
        """
        Mark a blob as processed by setting metadata, then delete it.

        Parameters
        ----------
        blob_path : str
            The blob path.
        """
        try:
            # First mark as processed
            metadata = {
                "processed": "true",
                "processed_at": datetime.now(UTC).isoformat(),
            }
            success = await self.azure_blob_manager.set_blob_metadata(blob_name=blob_path, metadata=metadata)
            if success:
                logger.info(f"Marked blob as processed: {blob_path}")
            else:
                logger.warning(f"Failed to mark blob as processed: {blob_path}")

        except Exception as e:
            logger.error(f"Error marking blob as processed {blob_path}: {e}")

    async def _mark_blob_with_error(self, blob_path: str, error_message: str) -> None:
        """
        Mark a blob with error information and increment retry count.

        Parameters
        ----------
        blob_path : str
            The blob path.
        error_message : str
            The error message to store.
        """
        try:
            # Get current metadata to preserve retry count
            current_metadata = await self.azure_blob_manager.get_blob_metadata(blob_path) or {}
            current_retry_count = int(current_metadata.get("retry_count", "0"))
            new_retry_count = current_retry_count + 1

            metadata = {
                "processed": "false",
                "retry_count": str(new_retry_count),
                "last_attempt": datetime.now(UTC).isoformat(),
                "last_error": error_message[:1000],  # Limit error message length
                "status": "retrying",  # Will be retried on next poll
            }
            await self.azure_blob_manager.set_blob_metadata(blob_name=blob_path, metadata=metadata)
            logger.info(f"Marked blob with error (attempt {new_retry_count}): {blob_path}")
        except Exception as e:
            logger.error(f"Error marking blob with error {blob_path}: {e}")

    async def _mark_blob_permanently_failed(self, blob_path: str, current_metadata: dict) -> None:
        """
        Mark a blob as permanently failed after exceeding retry limit.

        Parameters
        ----------
        blob_path : str
            The blob path.
        current_metadata : dict
            Current metadata from the blob.
        """
        try:
            metadata = {
                "processed": "false",
                "status": "permanently_failed",
                "retry_count": current_metadata.get("retry_count", "0"),
                "last_attempt": datetime.now(UTC).isoformat(),
                "last_error": current_metadata.get("last_error", "Max retries exceeded"),
                "failed_at": datetime.now(UTC).isoformat(),
            }
            await self.azure_blob_manager.set_blob_metadata(blob_name=blob_path, metadata=metadata)
            logger.error(
                f"Marked blob as permanently failed after "
                f"{current_metadata.get('retry_count', 0)} attempts: {blob_path}"
            )
        except Exception as e:
            logger.error(f"Error marking blob as permanently failed {blob_path}: {e}")

    def _should_delete_old_blob(self, metadata: dict) -> tuple[bool, str]:
        """
        Determine if an old blob should be deleted during startup cleanup.

        Parameters
        ----------
        metadata : dict
            The blob's metadata dictionary.

        Returns
        -------
        tuple[bool, str]
            (should_delete, reason) - True if should delete, with reason for logging.
        """
        # Case 1: Successfully processed blobs - safe to delete
        if metadata.get("processed") == "true":
            return True, "successfully processed"

        # Case 2: Legacy blob without metadata - delete for data protection
        # These are pre-migration blobs that never had metadata tracking
        if not metadata or ("processed" not in metadata and "retry_count" not in metadata and "status" not in metadata):
            return True, "legacy blob without metadata"

        # Case 3: Stale in_progress blobs (worker crashed or service restarted)
        # Reset them so they can be retried
        if metadata.get("status") == "in_progress":
            return False, "stale in_progress - will reset"

        # Case 4: Blob with retry metadata - keep for processing/investigation
        status = metadata.get("status", "unknown")
        retry_count = metadata.get("retry_count", "0")
        return False, f"has metadata (status={status}, retry_count={retry_count})"

    def _evaluate_blob_for_cleanup(self, blob: dict) -> tuple[bool, str | None]:
        """
        Evaluate if a blob should be included in cleanup.

        Parameters
        ----------
        blob : dict
            Blob information dictionary.

        Returns
        -------
        tuple[bool, str | None]
            (should_cleanup, reason) - True if should delete, reason for logging or None to skip.
        """
        blob_name = blob["name"]

        # Ensure blob is in user-uploads prefix
        if not blob_name.startswith(self.user_uploads_prefix):
            return False, None

        # Only consider audio files
        blob_path = Path(blob_name)
        if blob_path.suffix.lower() not in self.supported_extensions:
            return False, None

        # Determine if this old blob should be deleted based on metadata
        metadata = blob.get("metadata", {})
        should_delete, reason = self._should_delete_old_blob(metadata)
        return should_delete, reason

    async def _cleanup_old_blobs_on_startup(self) -> None:  # noqa: C901, PLR0912, PLR0915
        """
        Clean up old blobs from before the service startup time.

        This runs once on the first poll to:
        1. Remove old recordings that should have been cleaned up
        2. Reset stale in_progress blobs (from crashed workers)
        3. Ensure we only work with new uploads from this session
        """
        try:
            logger.info("Starting cleanup of old blobs from before service startup...")

            # List all blobs with user-uploads prefix (all users)
            all_blobs = await self.azure_blob_manager.list_blobs_in_prefix(
                prefix=self.user_uploads_prefix, include_metadata=True
            )

            # Evaluate blobs for cleanup
            old_blobs = []
            stale_in_progress_blobs = []

            for blob in all_blobs:
                should_delete, reason = self._evaluate_blob_for_cleanup(blob)

                if reason is None:  # Skip blobs that don't meet criteria
                    continue

                blob_name = blob["name"]
                if should_delete:
                    old_blobs.append(blob)
                    logger.info(f"Will clean up old blob ({reason}): {blob_name}")
                elif reason == "stale in_progress - will reset":
                    stale_in_progress_blobs.append(blob)
                    logger.info(f"Will reset stale in_progress blob: {blob_name}")
                else:
                    logger.info(f"Keeping old blob ({reason}): {blob_name}")

            # Delete old blobs
            if old_blobs:
                logger.info(f"Found {len(old_blobs)} old blob(s) to clean up")

                for blob in old_blobs:
                    blob_name = blob["name"]
                    try:
                        success = await self.azure_blob_manager.delete_blob(blob_name)
                        if success:
                            logger.info(f"Deleted old blob: {blob_name}")
                        else:
                            logger.warning(f"Failed to delete old blob: {blob_name}")
                    except Exception as e:
                        logger.error(f"Error deleting old blob {blob_name}: {e}")

                logger.info(f"Cleanup complete - deleted {len(old_blobs)} old blob(s)")
            else:
                logger.info("No old blobs found to clean up")

            # Reset stale in_progress blobs
            if stale_in_progress_blobs:
                logger.info(f"Found {len(stale_in_progress_blobs)} stale in_progress blob(s) to reset")

                for blob in stale_in_progress_blobs:
                    blob_name = blob["name"]
                    try:
                        current_metadata = blob.get("metadata", {})
                        retry_count = current_metadata.get("retry_count", "0")

                        # Reset status to allow reprocessing, but keep retry history
                        metadata = {
                            "processed": "false",
                            "status": "reset_from_stale",
                            "reset_at": datetime.now(UTC).isoformat(),
                            "retry_count": retry_count,  # ← Preserve retry count!
                        }
                        success = await self.azure_blob_manager.set_blob_metadata(
                            blob_name=blob_name, metadata=metadata
                        )
                        if success:
                            logger.info(f"Reset stale in_progress blob (retry_count={retry_count}): {blob_name}")
                        else:
                            logger.warning(f"Failed to reset stale blob: {blob_name}")
                    except Exception as e:
                        logger.error(f"Error resetting stale blob {blob_name}: {e}")

                logger.info(f"Reset complete - processed {len(stale_in_progress_blobs)} stale blob(s)")
            else:
                logger.info("No stale in_progress blobs found to reset")

        except Exception as e:
            logger.error(f"Error during startup cleanup: {e}")

    async def _worker(self, worker_id: int) -> None:
        """
        Worker coroutine that pulls blobs from the queue and processes them.

        On shutdown, the worker exits immediately. Any in-progress blobs will be
        recovered by the startup cleanup on next service start.

        Parameters
        ----------
        worker_id : int
            Unique identifier for this worker (for logging)
        """
        logger.info(f"Worker {worker_id} started")

        while not self._shutdown:
            try:
                # Wait for a blob from the queue (with timeout to check shutdown flag)
                try:
                    blob_info = await asyncio.wait_for(self.blob_queue.get(), timeout=1.0)
                except TimeoutError:
                    # No item in queue, loop back to check shutdown flag
                    continue

                try:
                    # Process the blob
                    logger.info(f"Worker {worker_id} processing blob: {blob_info['name']}")
                    await self.process_discovered_audio(blob_info)
                except Exception as e:
                    logger.error(f"Worker {worker_id} error processing blob {blob_info.get('name', 'unknown')}: {e}")
                finally:
                    # Mark task as done
                    self.blob_queue.task_done()

            except Exception as e:
                logger.error(f"Worker {worker_id} encountered unexpected error: {e}")

        logger.info(f"Worker {worker_id} stopped")

    async def run_polling_loop(self) -> None:
        """
        Run the continuous global polling loop with parallel workers.

        This method:
        1. Starts worker tasks to process blobs in parallel
        2. Polls for new audio files every 30 seconds
        3. Adds discovered files to a queue for workers to process
        4. Cleans up old blobs on first poll
        """
        logger.info(
            f"Starting global transcription polling service with {self.num_workers} workers "
            f"(interval: {self.polling_interval_seconds}s)"
        )

        # Start worker tasks
        for worker_id in range(self.num_workers):
            task = asyncio.create_task(self._worker(worker_id))
            self.worker_tasks.append(task)

        logger.info(f"Started {self.num_workers} worker tasks")

        try:
            while not self._shutdown:
                try:
                    # On first poll, clean up old blobs
                    if self._is_first_poll:
                        await self._cleanup_old_blobs_on_startup()
                        self._is_first_poll = False

                    # Poll for new files
                    unprocessed_files = await self.poll_for_new_audio_files()

                    # Add discovered files to queue for workers to process
                    for blob_info in unprocessed_files:
                        await self.blob_queue.put(blob_info)
                        logger.debug(f"Added blob to queue: {blob_info['name']}")

                except Exception as e:
                    logger.error(f"Error in polling loop: {e}")

                # Wait before next poll
                await asyncio.sleep(self.polling_interval_seconds)

        finally:
            # Fast shutdown: abandon queue and cancel workers immediately
            # Any in-progress blobs will be recovered by startup cleanup
            logger.info("Shutting down polling service (fast shutdown)...")
            self._shutdown = True

            # Cancel all worker tasks immediately
            logger.info("Cancelling worker tasks...")
            for task in self.worker_tasks:
                task.cancel()

            # Wait for all workers to finish cancellation
            await asyncio.gather(*self.worker_tasks, return_exceptions=True)
            logger.info("All workers stopped")

            # Close the blob manager to clean up credential resources
            logger.info("Closing Azure blob manager...")
            await self.azure_blob_manager.close()
            logger.info("Azure blob manager closed")
