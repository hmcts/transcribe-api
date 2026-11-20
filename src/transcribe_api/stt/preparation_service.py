"""PreparationService: converts and submits the uploads the API accepted without converting (DIAAT-408).

With the system flag ``async_upload_preparation`` on, ``POST /uploads`` stores the original file
and answers at once, and ``POST /jobs`` creates the job ``PENDING`` with ``source_blob_path`` set
and does not call Azure Speech. This service, a task of the worker beside the two pollers, then
does what the upload request used to do, for each such job:

1. read the original back from storage;
2. convert it to a 16 kHz mono WAV (``normalize_to_wav``) and check its exact length;
3. store the WAV at the job's ``audio_blob_path`` (the blob name the upload answered with);
4. submit the job to Azure Speech (``submit_existing_job``: the same code, events and failure
   handling as a job submitted by the request);
5. delete the original.

A job that cannot be converted, or is too long by the exact check, becomes a ``FAILED`` job with the
sentences the upload request gives for the same files. A storage failure is retried, up to
``PREPARE_MAX_ATTEMPTS`` times, and then fails the job. A crash, or a restart of the worker, leaves
the original in storage and the job ``PENDING``: it is claimed again once its lease has run out.

Claiming. A job is claimed by one conditional UPDATE that adds 1 to ``preparation_attempts`` and
stamps ``updated_datetime``; while the worker prepares it, it renews that stamp every
``PREPARE_HEARTBEAT_SECONDS``. A job with attempts > 0 is claimed again only when its stamp is older
than ``PREPARE_LEASE_SECONDS``, so two workers (or two API processes running workers) never prepare
one job at the same time, and a crashed one is taken over after the lease. A job whose attempts are
used up (the last one crashed, or kept failing) is failed by ``fail_exhausted``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from hmcts_otel import journey, stage
from sqlmodel import Session

from transcribe_api.domain import interface_recording as jobs
from transcribe_api.domain.models_recording import JobStatus, SpeechBatchJob
from transcribe_api.runtime.db_recording import get_engine
from transcribe_api.runtime.settings_recording import AZURE_BATCH_FILE_LIMIT_SECONDS, get_settings
from transcribe_api.runtime.telemetry import (
    JOB_PREPARE_OPERATION,
    JOURNEY_JOB,
    STAGE_PREPARATION,
    upload_id_from_blob,
)
from transcribe_api.stt import job_telemetry
from transcribe_api.stt.audio_storage import AudioStorage, get_audio_storage, source_extension
from transcribe_api.stt.batch_client import BatchSubmissionError
from transcribe_api.stt.preprocessing import AudioDecodeError, normalize_to_wav, wav_duration_seconds
from transcribe_api.stt.recording_limits import current_limit_seconds, too_long_detail
from transcribe_api.stt.speech_auth import SpeechAuthError
from transcribe_api.stt.submission import submit_existing_job

logger = logging.getLogger(__name__)

# The sentences a clerk reads on a failed job (web: error_message). The first two are the ones
# POST /uploads gives for the same files.
UNDECODABLE_MESSAGE = "The audio file could not be decoded. It may be corrupt or in an unsupported format."
SOURCE_MISSING_MESSAGE = "The uploaded recording could not be found. Please upload it again."
NOT_PREPARED_MESSAGE = "The recording could not be prepared for transcription. Please upload it again."

CODE_UNDECODABLE = "upload.undecodable"
CODE_TOO_LONG = "upload.too_long"
CODE_SOURCE_MISSING = "preparation.source_missing"
CODE_STORAGE_FAILED = "preparation.storage_failed"
CODE_TRANSCODER_UNAVAILABLE = "preparation.transcoder_unavailable"
CODE_EXHAUSTED = "preparation.exhausted"


class PreparationFailed(Exception):
    """The job cannot be prepared and trying again will not help: it ends FAILED with ``message``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class PreparationRetry(Exception):
    """Preparing the job failed for a reason that may pass (storage, the converter): try again."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ClaimedJob:
    """What preparing a job needs, copied out of its row (the row is read in short sessions)."""

    id: UUID
    attempt: int
    source_blob_path: str
    audio_blob_path: str
    created_datetime: datetime | None
    trace_parent: str | None


@dataclass(frozen=True)
class PreparedAudio:
    audio_url: str
    duration_seconds: float | None


def _claimed(job: SpeechBatchJob) -> ClaimedJob | None:
    if not job.source_blob_path or not job.audio_blob_path:
        return None
    return ClaimedJob(
        id=job.id,
        attempt=job.preparation_attempts,
        source_blob_path=job.source_blob_path,
        audio_blob_path=job.audio_blob_path,
        created_datetime=job.created_datetime,
        trace_parent=job.trace_parent,
    )


class PreparationService:
    """Background service that prepares the jobs ``POST /jobs`` deferred."""

    def __init__(self, storage: AudioStorage | None = None) -> None:
        self.settings = get_settings()
        self._storage = storage
        self._shutdown = False
        self._running: set[asyncio.Task] = set()

    @property
    def storage(self) -> AudioStorage:
        return self._storage if self._storage is not None else get_audio_storage()

    # The loop -----------------------------------------------------------------------------

    async def run_polling_loop(self) -> None:
        logger.info(
            "PreparationService started (interval=%ds, concurrency=%d)",
            self.settings.PREPARE_POLL_INTERVAL_SECONDS,
            self.settings.PREPARE_MAX_CONCURRENT,
        )
        try:
            while not self._shutdown:
                try:
                    await self.poll_once()
                except Exception as exc:
                    logger.error("Unexpected error in preparation loop (%s)", type(exc).__name__, exc_info=exc)
                await asyncio.sleep(self.settings.PREPARE_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("PreparationService cancelled")
            for task in list(self._running):
                task.cancel()
            await asyncio.gather(*self._running, return_exceptions=True)
            raise

    async def poll_once(self) -> int:
        """Fail the jobs whose attempts are used up, then start preparing jobs while there is room
        (``PREPARE_MAX_CONCURRENT``). Returns how many were started."""
        await asyncio.to_thread(self._fail_exhausted)
        started = 0
        while len(self._running) < self.settings.PREPARE_MAX_CONCURRENT:
            job = await asyncio.to_thread(self._claim)
            if job is None:
                break
            task = asyncio.create_task(self.prepare(job))
            self._running.add(task)
            task.add_done_callback(self._running.discard)
            started += 1
        return started

    async def wait_idle(self) -> None:
        """Wait for the jobs being prepared to finish (tests, shutdown)."""
        while self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    # Claiming -----------------------------------------------------------------------------

    def _claim(self) -> ClaimedJob | None:
        with Session(get_engine()) as session:
            job = jobs.claim_job_for_preparation(
                session,
                max_attempts=self.settings.PREPARE_MAX_ATTEMPTS,
                lease_seconds=self.settings.PREPARE_LEASE_SECONDS,
            )
            if job is None:
                return None
            claimed = _claimed(job)
            if claimed is None:
                # No blob names to work from (a row nothing in this service writes): never retried for ever.
                logger.error("Job %s waits for preparation but names no audio blob", job.id)
                jobs.mark_preparation_failed(session, job.id, NOT_PREPARED_MESSAGE)
            return claimed

    def _fail_exhausted(self) -> None:
        with Session(get_engine()) as session:
            failed = jobs.fail_exhausted_preparations(
                session,
                max_attempts=self.settings.PREPARE_MAX_ATTEMPTS,
                lease_seconds=self.settings.PREPARE_LEASE_SECONDS,
                message=NOT_PREPARED_MESSAGE,
            )
        for job in failed:
            logger.error("Job %s could not be prepared in %d attempts", job.id, job.preparation_attempts)
            with journey(JOURNEY_JOB, str(job.id), parent_id=upload_id_from_blob(job.audio_blob_path)):
                job_telemetry.record_job_finished("error", CODE_EXHAUSTED, created_at=job.created_datetime)

    # Preparing one job --------------------------------------------------------------------

    async def prepare(self, job: ClaimedJob) -> None:
        """Prepare one claimed job: convert, store the WAV, submit, tidy up. Never raises (except
        for cancellation): its outcome is the job's state."""
        # DIAAT-276: everything emitted while preparing this job carries its journey.
        with journey(JOURNEY_JOB, str(job.id), parent_id=upload_id_from_blob(job.audio_blob_path)):
            heartbeat = asyncio.create_task(self._heartbeat(job.id))
            try:
                try:
                    prepared = await self._convert_and_store(job)
                except PreparationFailed as exc:
                    await self._fail(job, exc.code, exc.message)
                    return
                except PreparationRetry as exc:
                    await self._retry_or_fail(job, exc.code)
                    return
                except asyncio.CancelledError:
                    # A worker stopping: hand the job back at once, so the next start takes it up.
                    await asyncio.shield(asyncio.to_thread(self._release, job.id, 0))
                    raise
                except Exception as exc:
                    logger.error("Preparing job %s failed (%s)", job.id, type(exc).__name__, exc_info=exc)
                    await self._retry_or_fail(job, f"unhandled.{type(exc).__name__}"[:64])
                    return
                await self._submit(job, prepared)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _heartbeat(self, job_id: UUID) -> None:
        interval = self.settings.PREPARE_HEARTBEAT_SECONDS
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(self._renew, job_id)
            except Exception as exc:
                logger.warning("Could not renew the lease of job %s (%s)", job_id, type(exc).__name__)

    def _renew(self, job_id: UUID) -> None:
        with Session(get_engine()) as session:
            jobs.renew_preparation(session, job_id)

    def _release(self, job_id: UUID, retry_after_seconds: float) -> None:
        with Session(get_engine()) as session:
            jobs.release_preparation(
                session,
                job_id,
                retry_after_seconds=retry_after_seconds,
                lease_seconds=self.settings.PREPARE_LEASE_SECONDS,
            )

    async def _convert_and_store(self, job: ClaimedJob) -> PreparedAudio:
        """Steps 1 to 3. The ``preparation`` stage (``preparation.started`` / ``.completed`` /
        ``.failed``) covers them; a failure that retrying will not mend ends it as a failure (the
        file's fault), one that may pass as an error (ours or storage's)."""
        storage = self.storage
        with stage(STAGE_PREPARATION, link_from=job.trace_parent) as preparation:
            try:
                with tempfile.TemporaryDirectory(prefix="prepare-") as tmpdir:
                    input_path = Path(tmpdir) / f"input{source_extension(job.source_blob_path)}"
                    wav_path = Path(tmpdir) / "output.wav"
                    await self._fetch(storage, job, input_path)
                    await self._convert(input_path, wav_path)
                    duration = await asyncio.to_thread(wav_duration_seconds, wav_path)
                    if duration is not None:
                        preparation.set_attribute("audio.duration_seconds", duration)
                        await self._check_length(duration)
                    await self._store(storage, job, wav_path)
                audio_url = await storage.url(job.audio_blob_path)
            except PreparationFailed as exc:
                preparation.fail(exc.code)
                raise
            except PreparationRetry as exc:
                preparation.error(exc.code)
                raise
            return PreparedAudio(audio_url=audio_url, duration_seconds=duration)

    async def _fetch(self, storage: AudioStorage, job: ClaimedJob, input_path: Path) -> None:
        try:
            await storage.download(job.source_blob_path, input_path)
        except FileNotFoundError as exc:
            raise PreparationFailed(CODE_SOURCE_MISSING, SOURCE_MISSING_MESSAGE) from exc
        except Exception as exc:
            logger.warning("Could not read the upload of job %s (%s)", job.id, type(exc).__name__)
            raise PreparationRetry(CODE_STORAGE_FAILED) from exc

    async def _convert(self, input_path: Path, wav_path: Path) -> None:
        try:
            await normalize_to_wav(input_path, wav_path)
        except AudioDecodeError as exc:
            raise PreparationFailed(CODE_UNDECODABLE, UNDECODABLE_MESSAGE) from exc
        except FileNotFoundError as exc:
            # ffmpeg is not installed: the worker is misconfigured, the file is fine.
            logger.error("Cannot convert an upload, ffmpeg is not available (%s)", type(exc).__name__)
            raise PreparationRetry(CODE_TRANSCODER_UNAVAILABLE) from exc

    async def _check_length(self, duration: float) -> None:
        # The exact check, on the WAV ffmpeg wrote: the length Azure will see. Until recordings
        # are split into parts (a later change) no job may exceed Azure's limit per file, whatever
        # the effective limit says.
        limit = min(await asyncio.to_thread(current_limit_seconds), AZURE_BATCH_FILE_LIMIT_SECONDS)
        if duration > limit:
            raise PreparationFailed(CODE_TOO_LONG, too_long_detail(duration, limit))

    async def _store(self, storage: AudioStorage, job: ClaimedJob, wav_path: Path) -> None:
        try:
            stored = await storage.upload(
                wav_path, job.audio_blob_path, concurrency=self.settings.BLOB_UPLOAD_CONCURRENCY
            )
        except Exception as exc:
            logger.warning("Could not store the audio of job %s (%s)", job.id, type(exc).__name__)
            raise PreparationRetry(CODE_STORAGE_FAILED) from exc
        if not stored:
            raise PreparationRetry(CODE_STORAGE_FAILED)

    # After the audio is stored -----------------------------------------------------------

    async def _submit(self, job: ClaimedJob, prepared: PreparedAudio) -> None:
        """Step 4, then 5. The job is submitted by the code the request used (submit_existing_job):
        a refusal by Azure Speech leaves it FAILED, with the sentences it always had. The original is
        deleted only once the job is settled (submitted, failed or gone): if the job could not even be
        updated it stays PENDING, and the next attempt needs the original."""
        try:
            attached = await asyncio.to_thread(self._attach_audio, job, prepared)
            if attached:
                await self._submit_to_speech(job)
            else:
                # Deleted (or taken over) while it was being prepared: nothing to submit, and the
                # WAV just stored belongs to no job.
                await self._delete_quietly(job.audio_blob_path, job.id)
        except Exception as exc:
            logger.error("Submitting job %s failed (%s)", job.id, type(exc).__name__, exc_info=exc)
        if await asyncio.to_thread(self._is_settled, job.id):
            await self._delete_quietly(job.source_blob_path, job.id)

    def _attach_audio(self, job: ClaimedJob, prepared: PreparedAudio) -> bool:
        """Record the server's own URL and the exact length on the job. False if the job is gone or
        is no longer waiting for this attempt."""
        with Session(get_engine()) as session:
            row = jobs.get_job_by_id(session, job.id)
            if row is None or row.status != JobStatus.PENDING or row.batch_job_id is not None:
                return False
            # Never the URL the client sent: the one the stored WAV has.
            row.audio_url = prepared.audio_url
            if row.audio_duration_seconds is None and prepared.duration_seconds is not None:
                row.audio_duration_seconds = prepared.duration_seconds
            jobs.save_job(session, row)
            return True

    def _is_settled(self, job_id: UUID) -> bool:
        """Whether nothing more will be prepared for the job: it is gone, submitted or failed."""
        with Session(get_engine()) as session:
            row = jobs.get_job_by_id(session, job_id)
            return row is None or row.status != JobStatus.PENDING or row.batch_job_id is not None

    async def _submit_to_speech(self, job: ClaimedJob) -> None:
        with Session(get_engine()) as session:
            row = jobs.get_job_by_id(session, job.id)
            if row is None:
                return
            try:
                await submit_existing_job(session, row)
            except (BatchSubmissionError, SpeechAuthError):
                # Azure Speech refused the job or could not be authenticated to: it is FAILED, with
                # its message, its key released and the cause logged (stt/submission.py).
                return

    async def _delete_quietly(self, blob_name: str, job_id: UUID) -> None:
        try:
            await self.storage.delete(blob_name)
        except Exception as exc:
            # Left for the storage lifecycle rule on incoming/; never fails the job.
            logger.warning("Could not delete a blob of job %s (%s)", job_id, type(exc).__name__)

    # Failing and retrying ----------------------------------------------------------------

    async def _fail(self, job: ClaimedJob, code: str, message: str) -> None:
        """End the job as FAILED with ``message`` (its key released, as for a refused submission),
        count it as finished, and remove the original: the user uploads again."""
        failed = await asyncio.to_thread(self._mark_failed, job.id, message)
        if failed:
            outcome = "failure" if code in (CODE_UNDECODABLE, CODE_TOO_LONG, CODE_SOURCE_MISSING) else "error"
            job_telemetry.record_job_finished(outcome, code, created_at=job.created_datetime)
        await self._delete_quietly(job.source_blob_path, job.id)

    def _mark_failed(self, job_id: UUID, message: str) -> bool:
        with Session(get_engine()) as session:
            return jobs.mark_preparation_failed(session, job_id, message)

    async def _retry_or_fail(self, job: ClaimedJob, code: str) -> None:
        if job.attempt >= self.settings.PREPARE_MAX_ATTEMPTS:
            await self._fail(job, code, NOT_PREPARED_MESSAGE)
            return
        job_telemetry.record_retry(JOB_PREPARE_OPERATION, job.attempt, code)
        backoff = self.settings.PREPARE_RETRY_BACKOFF_SECONDS * job.attempt
        await asyncio.to_thread(self._release, job.id, backoff)
