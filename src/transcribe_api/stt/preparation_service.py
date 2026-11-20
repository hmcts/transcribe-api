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

A recording over 240 minutes (``AZURE_BATCH_FILE_LIMIT_SECONDS``, Azure's limit per file) is not
submitted whole. After the length check it is cut into parts (stt/splitter.py: at the longest
silence near each equal-length boundary), the parts are recorded in ``transcription_job_part``,
and each is stored and submitted as its own Azure job, up to ``MAX_PARALLEL_PARTS`` at once, part 1
first and as soon as its blob is stored, while the full WAV (for playback) is stored beside them.
A part that fails is retried alone, ``PART_MAX_ATTEMPTS`` times, then fails the job with a message
naming the part. Resuming. The part rows are the durable record: a part with its Azure job recorded
is never stored or submitted again, so a crash or restart mid-way continues with the parts not yet
submitted (the cut is made again at the recorded offsets and checked against the recorded
lengths). The one window left is a crash after Azure accepted a part and before its row was
updated: that part is submitted again and Azure holds an Azure job nothing polls. The job stays
``PENDING`` until every part is submitted, then ``SUBMITTED``. A job deleted meanwhile
(``DELETE /jobs/{id}``) has its part blobs and Azure jobs deleted; the worker finds the part rows
gone at its next write, deletes what it had just made, and stops.

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
    PART_SUBMIT_OPERATION,
    STAGE_PREPARATION,
    STAGE_SPLITTING,
    upload_id_from_blob,
)
from transcribe_api.stt import job_telemetry
from transcribe_api.stt.audio_storage import AudioStorage, get_audio_storage, source_extension
from transcribe_api.stt.batch_client import BatchSubmissionError, delete_batch_job
from transcribe_api.stt.part_submission import cleanup_job_parts, part_blob_name, submit_part_to_speech
from transcribe_api.stt.preprocessing import AudioDecodeError, normalize_to_wav, wav_duration_seconds
from transcribe_api.stt.recording_limits import current_limit_seconds, too_long_detail
from transcribe_api.stt.speech_auth import SpeechAuthError
from transcribe_api.stt.splitter import CutPart, SplitError, cut_segments, split_wav
from transcribe_api.stt.submission import record_parts_queued, submit_existing_job

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
CODE_SPLIT_FAILED = "split.failed"
CODE_SPLIT_MISMATCH = "preparation.split_mismatch"
# A resumed attempt cuts the recording again at the offsets recorded for its parts; a part this far
# from its recorded length means the audio is not the audio the parts were recorded for.
SPLIT_MISMATCH_TOLERANCE_SECONDS = 0.05


class PreparationFailed(Exception):
    """The job cannot be prepared and trying again will not help: it ends FAILED with ``message``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class PartFailed(Exception):
    """A part could not be stored and submitted in ``PART_MAX_ATTEMPTS`` attempts: the job fails."""

    def __init__(self, part_index: int, part_count: int, code: str) -> None:
        super().__init__(code)
        self.part_index = part_index
        self.part_count = part_count
        self.code = code

    @property
    def message(self) -> str:
        return (
            f"Part {self.part_index} of {self.part_count} of the recording could not be sent for transcription. "
            "Please upload the recording again."
        )


class JobGone(Exception):
    """The job was deleted while its parts were being prepared."""


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
    # A recording over 240 minutes: the full WAV (not stored yet) and the parts cut from it, with the
    # way each part's start was chosen. Empty for a recording that is submitted whole.
    wav_path: Path | None = None
    parts: tuple[CutPart, ...] = ()
    cut_kinds: tuple[str, ...] = ()


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
        for failed in await asyncio.to_thread(self._fail_exhausted):
            await self._cleanup_parts(failed.id)
        self._prune()
        # All the jobs there is room for are claimed in one go, before any of them is started: a job
        # an attempt hands back is not claimed again by the same poll.
        room = self.settings.PREPARE_MAX_CONCURRENT - len(self._running)
        claimed = await asyncio.to_thread(self._claim_many, room) if room > 0 else []
        for job in claimed:
            task = asyncio.create_task(self.prepare(job))
            self._running.add(task)
            task.add_done_callback(self._running.discard)
        return len(claimed)

    async def wait_idle(self) -> None:
        """Wait for the jobs being prepared to finish (tests, shutdown)."""
        while self._running:
            self._prune()
            if self._running:
                await asyncio.wait(list(self._running), timeout=1.0)
            else:
                await asyncio.sleep(0)

    def _prune(self) -> None:
        """Forget the tasks that have finished. Their done-callback does this too; doing it here as well
        means a caller waiting on ``_running`` never depends on a callback having run."""
        self._running.difference_update({task for task in self._running if task.done()})

    # Claiming -----------------------------------------------------------------------------

    def _claim_many(self, count: int) -> list[ClaimedJob]:
        claimed: list[ClaimedJob] = []
        while len(claimed) < count and (job := self._claim()) is not None:
            claimed.append(job)
        return claimed

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

    def _fail_exhausted(self) -> list[jobs.FailedPreparation]:
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
        return failed

    # Preparing one job --------------------------------------------------------------------

    async def prepare(self, job: ClaimedJob) -> None:
        """Prepare one claimed job: convert, store the WAV, submit, tidy up. Never raises (except
        for cancellation): its outcome is the job's state."""
        # DIAAT-276: everything emitted while preparing this job carries its journey.
        with journey(JOURNEY_JOB, str(job.id), parent_id=upload_id_from_blob(job.audio_blob_path)):
            heartbeat = asyncio.create_task(self._heartbeat(job.id))
            try:
                # The temporary files (the original, the WAV, the parts) live until the job is submitted.
                with tempfile.TemporaryDirectory(prefix="prepare-") as tmpdir:
                    try:
                        prepared = await self._convert_and_store(job, Path(tmpdir))
                        await self._submit(job, prepared)
                    except PreparationFailed as exc:
                        await self._fail(job, exc.code, exc.message)
                    except PartFailed as exc:
                        await self._fail(job, exc.code, exc.message)
                    except PreparationRetry as exc:
                        await self._retry_or_fail(job, exc.code)
                    except JobGone:
                        logger.info("Job %s was deleted while it was being prepared", job.id)
                        await self._delete_quietly(job.source_blob_path, job.id)
                    except asyncio.CancelledError:
                        # A worker stopping: hand the job back at once, so the next start takes it up.
                        await asyncio.shield(asyncio.to_thread(self._release, job.id, 0))
                        raise
                    except Exception as exc:
                        logger.error("Preparing job %s failed (%s)", job.id, type(exc).__name__, exc_info=exc)
                        await self._retry_or_fail(job, f"unhandled.{type(exc).__name__}"[:64])
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

    async def _convert_and_store(self, job: ClaimedJob, tmp: Path) -> PreparedAudio:
        """Steps 1 to 3 (and, for a recording over 240 minutes, the cutting into parts instead of
        storing the WAV: that is stored beside the parts, see ``_submit_parts``). The ``preparation``
        stage (``preparation.started`` / ``.completed`` / ``.failed``) covers them; a failure that
        retrying will not mend ends it as a failure (the file's fault), one that may pass as an error
        (ours or storage's)."""
        storage = self.storage
        with stage(STAGE_PREPARATION, link_from=job.trace_parent) as preparation:
            try:
                input_path = tmp / f"input{source_extension(job.source_blob_path)}"
                wav_path = tmp / "output.wav"
                await self._fetch(storage, job, input_path)
                await self._convert(input_path, wav_path)
                input_path.unlink(missing_ok=True)  # the disk holds the WAV and its parts, not three copies
                duration = await asyncio.to_thread(wav_duration_seconds, wav_path)
                if duration is not None:
                    preparation.set_attribute("audio.duration_seconds", duration)
                    await self._check_length(duration)
                audio_url = await storage.url(job.audio_blob_path)
                if duration is not None and duration > AZURE_BATCH_FILE_LIMIT_SECONDS:
                    parts, kinds = await self._split(job, wav_path, duration, tmp)
                    return PreparedAudio(audio_url, duration, wav_path, tuple(parts), tuple(kinds))
                await self._store(storage, job, wav_path)
            except PreparationFailed as exc:
                preparation.fail(exc.code)
                raise
            except PreparationRetry as exc:
                preparation.error(exc.code)
                raise
            return PreparedAudio(audio_url=audio_url, duration_seconds=duration)

    async def _split(
        self, job: ClaimedJob, wav_path: Path, duration: float, tmp: Path
    ) -> tuple[list[CutPart], list[str]]:
        """Cut the WAV into parts, in the ``splitting`` stage. On a first attempt the parts are
        planned (stt/splitter.py); when parts are already recorded for the job (an attempt that was
        interrupted) the WAV is cut again at their recorded offsets, so the parts already submitted
        stay the parts of this recording."""
        parts_dir = tmp / "parts"
        parts_dir.mkdir()
        limit = AZURE_BATCH_FILE_LIMIT_SECONDS
        recorded = await asyncio.to_thread(self._recorded_parts, job.id)
        with stage(STAGE_SPLITTING, attributes={"audio.duration_seconds": duration}) as splitting:
            try:
                if recorded:
                    cut = await cut_segments(
                        wav_path, [r[0] for r in recorded[1:]], parts_dir, total_seconds=duration, limit=limit
                    )
                    kinds = [r[2] for r in recorded]
                    for part, (_, length, _) in zip(cut, recorded, strict=True):
                        if abs(part.duration_seconds - length) > SPLIT_MISMATCH_TOLERANCE_SECONDS:
                            splitting.error(CODE_SPLIT_MISMATCH)
                            raise PreparationRetry(CODE_SPLIT_MISMATCH)
                else:
                    plan, cut = await split_wav(
                        wav_path, duration, parts_dir, target=self.settings.PART_TARGET_SECONDS, limit=limit
                    )
                    kinds = [p.cut_kind for p in plan]
            except SplitError as exc:
                logger.error("Splitting the recording of job %s failed (%s)", job.id, exc)
                splitting.error(CODE_SPLIT_FAILED)
                raise PreparationRetry(CODE_SPLIT_FAILED) from exc
            splitting.set_attribute("split.part_count", len(cut))
            splitting.set_attribute("split.forced_cut_count", sum(1 for k in kinds if k == "forced"))
        return cut, kinds

    def _recorded_parts(self, job_id: UUID) -> list[tuple[float, float, str]]:
        """(offset, length, cut kind) of the parts already recorded for the job, in order."""
        with Session(get_engine()) as session:
            return [(p.offset_seconds, p.duration_seconds, p.cut_kind) for p in jobs.get_job_parts(session, job_id)]

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
        # The exact check, on the WAV ffmpeg wrote: the length Azure will see. The effective limit is
        # 240 minutes unless both flags are on (stt/recording_limits.py); only then can a longer
        # recording get here, and it is split into parts before it is submitted.
        limit = await asyncio.to_thread(current_limit_seconds)
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
            if not attached:
                # Deleted (or taken over) while it was being prepared: nothing to submit, and the
                # WAV just stored belongs to no job.
                await self._delete_quietly(job.audio_blob_path, job.id)
            elif prepared.parts:
                await self._submit_parts(job, prepared)
            else:
                await self._submit_to_speech(job)
        except (PartFailed, PreparationFailed, PreparationRetry, JobGone, asyncio.CancelledError):
            raise
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

    def _record_parts(self, job: ClaimedJob, prepared: PreparedAudio) -> list[jobs.TranscriptionJobPart]:
        """Record the parts (or take the ones an interrupted attempt recorded)."""
        plans = [
            jobs.PartPlan(
                part_index=part.index,
                part_count=len(prepared.parts),
                offset_seconds=part.offset_seconds,
                duration_seconds=part.duration_seconds,
                cut_kind=kind,
                blob_path=part_blob_name(job.audio_blob_path, part.index),
            )
            for part, kind in zip(prepared.parts, prepared.cut_kinds, strict=True)
        ]
        with Session(get_engine()) as session:
            return jobs.create_job_parts(session, job.id, plans)

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

    # A recording in parts ------------------------------------------------------------------

    async def _submit_parts(self, job: ClaimedJob, prepared: PreparedAudio) -> None:
        """Record the parts, then store and submit them with the full WAV, all at once: each part is
        submitted as soon as its own blob is stored (part 1 does not wait for the others or for the
        full WAV), at most ``MAX_PARALLEL_PARTS`` parts at a time. The first failure stops the rest.
        Once every part is with Azure Speech the job is SUBMITTED."""
        rows = await asyncio.to_thread(self._record_parts, job, prepared)
        files = {part.index: part.path for part in prepared.parts}
        slots = asyncio.Semaphore(self.settings.MAX_PARALLEL_PARTS)

        async def one(row: jobs.TranscriptionJobPart) -> None:
            async with slots:
                await self._store_and_submit_part(job, row, files[row.part_index])

        async def full_wav() -> None:
            await self._store(self.storage, job, prepared.wav_path)

        tasks = [asyncio.create_task(full_wav()), *(asyncio.create_task(one(row)) for row in rows)]
        try:
            await asyncio.gather(*tasks)
        except BaseException as exc:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(exc, JobGone):
                await self._forget_deleted_job(job, len(rows))
            raise
        if not await asyncio.to_thread(self._settle_parts, job, len(rows)):
            await self._forget_deleted_job(job, len(rows))
            raise JobGone

    def _settle_parts(self, job: ClaimedJob, part_count: int) -> bool:
        with Session(get_engine()) as session:
            settled = jobs.settle_parts_submitted(session, job.id)
            row = jobs.get_job_by_id(session, job.id)
            if settled and row is not None:
                record_parts_queued(row, part_count)
            return row is not None  # False only when the job has been deleted

    async def _forget_deleted_job(self, job: ClaimedJob, part_count: int) -> None:
        """The job is gone: what the worker stored for it belongs to no job."""
        names = [job.audio_blob_path, *(part_blob_name(job.audio_blob_path, i + 1) for i in range(part_count))]
        for name in names:
            await self._delete_quietly(name, job.id)

    async def _store_and_submit_part(self, job: ClaimedJob, row: jobs.TranscriptionJobPart, path: Path) -> None:
        """Store one part's blob and submit it to Azure Speech, retrying this part alone (its attempts
        are counted in its row, so an interrupted preparation continues the count). A part whose Azure
        job is recorded is never touched again."""
        if row.batch_job_id:
            return  # submitted by an earlier attempt
        storage = self.storage
        max_attempts = self.settings.PART_MAX_ATTEMPTS
        if row.attempts >= max_attempts:
            raise PartFailed(row.part_index, row.part_count, CODE_STORAGE_FAILED)
        locale, diarization = await asyncio.to_thread(self._locale_of, job.id)
        while True:
            attempt = await asyncio.to_thread(self._start_attempt, row.id)
            if attempt is None:
                if await asyncio.to_thread(self._part_exists, row.id):
                    return  # submitted meanwhile
                raise JobGone
            created: tuple[str, str] | None = None
            try:
                stored = await storage.upload(path, row.blob_path, concurrency=self.settings.BLOB_UPLOAD_CONCURRENCY)
                if not stored:
                    raise OSError("the part could not be stored")
                url = await storage.url(row.blob_path)
                created = await submit_part_to_speech(
                    job_id=job.id,
                    audio_url=url,
                    part_index=row.part_index,
                    part_count=row.part_count,
                    locale=locale,
                    enable_diarization=diarization,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = _part_failure_code(exc)
                logger.warning(
                    "Part %d of job %s failed (%s), attempt %d", row.part_index, job.id, type(exc).__name__, attempt
                )
                if attempt >= max_attempts:
                    await asyncio.to_thread(self._mark_part_failed, row.id, code)
                    raise PartFailed(row.part_index, row.part_count, code) from exc
                job_telemetry.record_retry(PART_SUBMIT_OPERATION, attempt, code, {"job.part_index": row.part_index})
                await asyncio.sleep(self.settings.PART_RETRY_BACKOFF_SECONDS * attempt)
                continue
            if not await asyncio.to_thread(self._record_part_submitted, row.id, *created):
                # The job was deleted meanwhile: the Azure job and the blob just made belong to nobody.
                with contextlib.suppress(Exception):
                    await delete_batch_job(created[1])
                await self._delete_quietly(row.blob_path, job.id)
                raise JobGone
            return

    def _locale_of(self, job_id: UUID) -> tuple[str, bool]:
        with Session(get_engine()) as session:
            row = jobs.get_job_by_id(session, job_id)
            if row is None:
                raise JobGone
            return row.locale, bool(row.enable_diarization)

    def _start_attempt(self, part_id: UUID) -> int | None:
        with Session(get_engine()) as session:
            return jobs.start_part_attempt(session, part_id)

    def _part_exists(self, part_id: UUID) -> bool:
        with Session(get_engine()) as session:
            return session.get(jobs.TranscriptionJobPart, part_id) is not None

    def _record_part_submitted(self, part_id: UUID, batch_job_id: str, batch_job_url: str) -> bool:
        with Session(get_engine()) as session:
            return jobs.mark_part_submitted(session, part_id, batch_job_id, batch_job_url)

    def _mark_part_failed(self, part_id: UUID, code: str) -> None:
        with Session(get_engine()) as session:
            jobs.mark_part_failed(session, part_id, code)

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
        await self._cleanup_parts(job.id)
        await self._delete_quietly(job.source_blob_path, job.id)

    async def _cleanup_parts(self, job_id: UUID) -> None:
        """A failed job's part blobs and Azure jobs are deleted (best effort: a job that has none, as
        every job of 240 minutes or less, costs one query)."""
        try:
            await cleanup_job_parts(job_id, storage=self.storage, engine=get_engine())
        except Exception as exc:
            logger.warning("Could not clean up the parts of job %s (%s)", job_id, type(exc).__name__)

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


def _part_failure_code(exc: BaseException) -> str:
    """The controlled code for a part that could not be stored or submitted (never the message)."""
    if isinstance(exc, BatchSubmissionError):
        return "speech.submission_rejected"
    if isinstance(exc, SpeechAuthError):
        return "speech.auth_failed"
    if isinstance(exc, OSError):
        return CODE_STORAGE_FAILED
    return f"unhandled.{type(exc).__name__}"[:64]
