"""
Transcription Polling Service for automatic audio file discovery and processing.

This service polls Azure Blob Storage for new audio files and automatically triggers
transcription processing without requiring frontend API calls. This avoids JWT timeout
issues on long audio recordings.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session

from transcribe_api.domain.blob_ownership import (
    AmbiguousBlobOwnerError,
    blob_owner_segment,
    resolve_blob_owner,
)
from transcribe_api.domain.exceptions import OwnershipConflictError
from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager, _redact_blob_path
from transcribe_api.stt.pipeline import transcribe_and_generate_llm_output
from transcribe_api.stt.utils import extract_transcription_id_from_blob_path
from transcribe_api.runtime.db_dictation import engine
from transcribe_api.domain.models_dictation import User
from transcribe_api.runtime.logger import logger
from transcribe_api.runtime.settings_dictation import get_settings

# A claim (status=in_progress) older than this is taken to be abandoned by a worker that
# died or was replaced mid-transcription, and may be claimed again. Claims are not renewed
# while held, so this must comfortably exceed the longest processing time: the speech
# request alone may take two 900s attempts (stt/fast_client.py), then LLM generation.
STALE_CLAIM_AFTER = timedelta(hours=2)


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
        self.startup_time = datetime.now(UTC)
        self._is_first_poll = True

        # Queue for discovered blobs to be processed
        self.blob_queue: asyncio.Queue = asyncio.Queue()
        self.num_workers = 20
        self.worker_tasks: list[asyncio.Task] = []
        self._shutdown = False

        logger.info(
            f"Global polling service initialized with {self.num_workers} workers at {self.startup_time.isoformat()}"
        )

    @staticmethod
    def _is_stale_claim(metadata: dict, last_modified: datetime | None) -> bool:
        """Whether an in_progress claim is old enough to have been abandoned.

        The claim time is the started_at written by the claim; failing that, the blob's
        last-modified time (setting metadata updates it). With neither, the claim is
        treated as live: wrongly leaving it costs a delay, wrongly taking it a duplicate.
        """
        claimed_at = last_modified
        try:
            claimed_at = datetime.fromisoformat(metadata["started_at"])
        except (KeyError, TypeError, ValueError):
            pass
        if claimed_at is None:
            return False
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - claimed_at > STALE_CLAIM_AFTER

    def _claim_refusal(self, metadata: dict, last_modified: datetime | None) -> str | None:
        """Why a blob in this metadata state must not be claimed, or None if it may be."""
        if metadata.get("processed") == "true":
            return "already processed"
        if metadata.get("status") == "permanently_failed":
            return "permanently failed"
        if metadata.get("status") == "in_progress" and not self._is_stale_claim(metadata, last_modified):
            return "claimed by a live worker"
        return None

    def _should_skip_blob(self, blob: dict) -> bool:
        """
        Check if a blob should be skipped when looking for files to process.

        Used during polling to filter out blobs that should NOT be transcribed:
        - Already successfully processed blobs
        - Permanently failed blobs (exceeded retry limit)
        - Currently being processed blobs (in_progress status, unless the claim is stale)
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

        # New uploads have no metadata and are not skipped; nor are "retrying" blobs,
        # "reset_from_stale" ones (written by earlier versions) or stale claims.
        refusal = self._claim_refusal(blob.get("metadata", {}), blob.get("last_modified"))
        if refusal:
            logger.debug(f"Skipping blob ({refusal}): {_redact_blob_path(blob_name)}")
            return True
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
                        f"Blob {_redact_blob_path(blob['name'])} has exceeded max retries "
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

    def resolve_blob_owner_from_path(self, blob_path: str) -> User | None:
        """
        Find the existing user who owns an upload blob.

        Uploads are written to user-uploads/{azure_user_id}/{filename}; legacy blobs to
        user-uploads/{email}/{filename}. The rules are shared with the API's ownership
        check (domain.blob_ownership). Users are never created here.

        Parameters
        ----------
        blob_path : str
            The full blob path.

        Returns
        -------
        User | None
            The owning User, or None if the path is malformed or no user matches.

        Raises
        ------
        AmbiguousBlobOwnerError
            If a legacy email segment matches more than one user.
        """
        segment = blob_owner_segment(blob_path)
        if segment is None:
            return None

        with Session(engine) as session:
            return resolve_blob_owner(session, segment)

    async def process_discovered_audio(self, blob_info: dict) -> bool:
        """
        Process a discovered audio file.

        This method:
        1. Marks blob as in_progress to prevent duplicate processing
        2. Resolves the owning user from the blob path (object id, or legacy email)
        3. Triggers transcription processing
        4. Marks blob as processed

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
        logger.info(f"Processing discovered audio file: {_redact_blob_path(blob_path)}")

        try:
            # Claim the blob first. If this returns False another worker holds (or has
            # finished) it, or it could not be claimed; either way it is not ours to process.
            marked = await self._mark_blob_in_progress(blob_path)
            if not marked:
                logger.info(f"Blob not claimed, skipping: {_redact_blob_path(blob_path)}")
                return False

            try:
                user = self.resolve_blob_owner_from_path(blob_path)
            except AmbiguousBlobOwnerError:
                # Neither the log nor the stored error carries the segment: legacy segments are emails.
                error_msg = "Blob owner is ambiguous: legacy email segment matches more than one user"
                logger.error(f"{error_msg}; not transcribing")
                await self._mark_blob_with_error(blob_path, error_msg)
                return False

            if not user:
                error_msg = "No user found for blob owner segment"
                logger.error(f"{error_msg}; not transcribing")
                await self._mark_blob_with_error(blob_path, error_msg)
                return False

            # Trigger transcription processing
            transcription_id = extract_transcription_id_from_blob_path(blob_path, str(user.id))

            logger.info("Starting transcription id=%s user_id=%s", transcription_id, user.id)

            await transcribe_and_generate_llm_output(
                user_upload_blob_storage_file_key=blob_path,
                user_id=user.id,
                user_email=user.email,
                azure_user_id=str(user.azure_user_id),
                transcription_id=transcription_id,
            )

            # Mark blob as processed
            await self._mark_blob_as_processed_and_soft_delete(blob_path)

            logger.info("Successfully processed transcription id=%s user_id=%s", transcription_id, user.id)

        except OwnershipConflictError:
            # SEC-1: the file name names another user's transcription. Never retried.
            logger.warning(
                "UNAUTHORISED_ACCESS_ATTEMPT reason=upload_names_other_users_transcription blob=%s",
                _redact_blob_path(blob_path),
            )
            await self._mark_blob_permanently_failed(blob_path, {"last_error": "OwnershipConflictError"})
            return False
        except Exception as e:
            logger.error("Error processing audio file %s: %s", _redact_blob_path(blob_path), e)
            await self._mark_blob_with_error(blob_path, str(e))
            return False
        else:
            return True

    async def _mark_blob_in_progress(self, blob_path: str) -> bool:
        """
        Mark a blob as currently being processed to prevent duplicate processing.

        This is the claim. It reads the blob's current state and writes the claim only
        if the blob is unchanged since that read (ETag If-Match), so when two pollers
        (e.g. the old and new pod of a rolling update, or a blob queued twice) race for
        the same blob, exactly one claim succeeds. A stale claim (see STALE_CLAIM_AFTER)
        is taken over the same way.

        Parameters
        ----------
        blob_path : str
            The blob path.

        Returns
        -------
        bool
            True if this poller now holds the claim, False otherwise.
        """
        try:
            current = await self.azure_blob_manager.get_blob_properties(blob_path)
            if current is None:
                logger.warning(f"Blob could not be read, not claiming: {_redact_blob_path(blob_path)}")
                return False

            current_metadata = current["metadata"]
            refusal = self._claim_refusal(current_metadata, current["last_modified"])
            if refusal:
                logger.info(f"Not claiming blob ({refusal}): {_redact_blob_path(blob_path)}")
                return False
            if current_metadata.get("status") == "in_progress":
                logger.warning(
                    f"Reclaiming stale claim (started_at={current_metadata.get('started_at')}, "
                    f"retry_count={current_metadata.get('retry_count', '0')}): {_redact_blob_path(blob_path)}"
                )

            metadata = {
                "processed": "false",
                "status": "in_progress",
                "started_at": datetime.now(UTC).isoformat(),
                "retry_count": current_metadata.get("retry_count", "0"),
            }
            success = await self.azure_blob_manager.set_blob_metadata(
                blob_name=blob_path, metadata=metadata, etag=current["etag"]
            )
            if success:
                logger.info(f"Marked blob as in_progress: {_redact_blob_path(blob_path)}")
                return True
            else:
                logger.info(f"Claim not written (blob changed by another worker, or write failed): {_redact_blob_path(blob_path)}")
                return False

        except Exception as e:
            logger.error(f"Error marking blob as in_progress {_redact_blob_path(blob_path)}: {e}")
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
                logger.info(f"Marked blob as processed: {_redact_blob_path(blob_path)}")
            else:
                logger.warning(f"Failed to mark blob as processed: {_redact_blob_path(blob_path)}")

        except Exception as e:
            logger.error(f"Error marking blob as processed {_redact_blob_path(blob_path)}: {e}")

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
            logger.info(f"Marked blob with error (attempt {new_retry_count}): {_redact_blob_path(blob_path)}")
        except Exception as e:
            logger.error(f"Error marking blob with error {_redact_blob_path(blob_path)}: {e}")

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
                f"{current_metadata.get('retry_count', 0)} attempts: {_redact_blob_path(blob_path)}"
            )
        except Exception as e:
            logger.error(f"Error marking blob as permanently failed {_redact_blob_path(blob_path)}: {e}")

    def _should_delete_old_blob(self, metadata: dict) -> tuple[bool, str]:
        """
        Determine if a blob should be deleted during startup cleanup.

        Only audio already marked processed is deleted: its transcript and document
        exist, and process_discovered_audio leaves deleting it to this cleanup. Every
        other blob is kept. New uploads have no metadata until claimed, so a blob
        without metadata is unprocessed work, not legacy junk. Claims are not reset here;
        a stale one is taken over by the next claim (see _mark_blob_in_progress).

        Parameters
        ----------
        metadata : dict
            The blob's metadata dictionary.

        Returns
        -------
        tuple[bool, str]
            (should_delete, reason) - True if should delete, with reason for logging.
        """
        if metadata.get("processed") == "true":
            return True, "successfully processed"

        status = metadata.get("status", "unclaimed")
        retry_count = metadata.get("retry_count", "0")
        return False, f"not processed (status={status}, retry_count={retry_count})"

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

    async def _cleanup_old_blobs_on_startup(self) -> None:
        """
        Delete processed audio uploads; runs once, on the first poll.

        Nothing else is touched: unprocessed uploads, retrying and failed blobs, and
        in_progress claims (which may be held by another worker that is still running,
        e.g. the other pod of a rolling update) are left for the polling loop.
        """
        try:
            logger.info("Starting startup cleanup of processed audio uploads...")

            all_blobs = await self.azure_blob_manager.list_blobs_in_prefix(
                prefix=self.user_uploads_prefix, include_metadata=True
            )

            processed_blobs = []
            for blob in all_blobs:
                should_delete, reason = self._evaluate_blob_for_cleanup(blob)
                if reason is None:  # not an audio upload
                    continue
                if should_delete:
                    processed_blobs.append(blob)
                else:
                    logger.info(f"Keeping blob ({reason}): {_redact_blob_path(blob['name'])}")

            if not processed_blobs:
                logger.info("No processed audio uploads to delete")
                return

            deleted = 0
            for blob in processed_blobs:
                blob_name = blob["name"]
                try:
                    # Conditional on the listed ETag: if the blob changed since the
                    # listing, it is no longer known to be processed and is kept.
                    if await self.azure_blob_manager.delete_blob(blob_name, etag=blob.get("etag")):
                        deleted += 1
                        logger.info(f"Deleted processed audio upload: {_redact_blob_path(blob_name)}")
                    else:
                        logger.warning(f"Did not delete processed audio upload: {_redact_blob_path(blob_name)}")
                except Exception as e:
                    logger.error(f"Error deleting processed audio upload {_redact_blob_path(blob_name)}: {e}")

            logger.info(f"Startup cleanup complete - deleted {deleted} of {len(processed_blobs)} processed upload(s)")

        except Exception as e:
            logger.error(f"Error during startup cleanup: {e}")

    async def _worker(self, worker_id: int) -> None:
        """
        Worker coroutine that pulls blobs from the queue and processes them.

        On shutdown, the worker exits immediately. Its in-progress claims are taken
        over by a poller once they are stale (STALE_CLAIM_AFTER).

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
                    logger.info(f"Worker {worker_id} processing blob: {_redact_blob_path(blob_info['name'])}")
                    await self.process_discovered_audio(blob_info)
                except Exception as e:
                    logger.error(f"Worker {worker_id} error processing blob {_redact_blob_path(blob_info.get('name', 'unknown'))}: {e}")
                finally:
                    # Mark task as done
                    self.blob_queue.task_done()

            except Exception as e:
                logger.error(f"Worker {worker_id} encountered unexpected error: {e}")

        logger.info(f"Worker {worker_id} stopped")

    async def poll_cycle(self) -> list[dict]:
        """Run the startup cleanup on the first call, then poll for blobs to process."""
        if self._is_first_poll:
            await self._cleanup_old_blobs_on_startup()
            self._is_first_poll = False
        return await self.poll_for_new_audio_files()

    async def run_polling_loop(self) -> None:
        """
        Run the continuous global polling loop with parallel workers.

        This method:
        1. Starts worker tasks to process blobs in parallel
        2. Polls for new audio files every 30 seconds
        3. Adds discovered files to a queue for workers to process
        4. Deletes processed audio uploads on first poll
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
                    unprocessed_files = await self.poll_cycle()

                    # Add discovered files to queue for workers to process
                    for blob_info in unprocessed_files:
                        await self.blob_queue.put(blob_info)
                        logger.debug(f"Added blob to queue: {_redact_blob_path(blob_info['name'])}")

                except Exception as e:
                    logger.error(f"Error in polling loop: {e}")

                # Wait before next poll
                await asyncio.sleep(self.polling_interval_seconds)

        finally:
            # Fast shutdown: abandon queue and cancel workers immediately
            # Abandoned claims are taken over by a poller once stale (STALE_CLAIM_AFTER)
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
