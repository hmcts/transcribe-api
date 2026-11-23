"""Submitting the parts of a recording longer than 240 minutes, and cleaning them up (DIAAT-408).

Each part is its own Azure batch job (``submit_part_to_speech``). The worker's preparation
(stt/preparation_service.py) stores and submits the parts with bounded parallelism and records each
in ``transcription_job_part``; this module is the Azure and storage side of one part, with the same
telemetry as a job submitted by its request: a ``speech_submission`` stage per part, carrying
``job.part_index`` and ``job.part_count``, and the Azure job's ID as the dependency reference.

``cleanup_job_parts`` removes every part blob and Azure job of a job (each step on its own): used
when a job fails, and by ``delete_job_and_parts`` when a job is deleted, which removes the job and
its part rows only once every resource they record has been deleted.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from hmcts_otel import stage
from sqlmodel import Session

from transcribe_api.domain import interface_recording as jobs
from transcribe_api.runtime.db_recording import get_engine
from transcribe_api.runtime.telemetry import batch_job_id_from_url
from transcribe_api.stt.audio_storage import AudioStorage, get_audio_storage
from transcribe_api.stt.batch_client import BatchSubmissionError, delete_batch_job, submit_batch_job
from transcribe_api.stt.speech_auth import SpeechAuthError

logger = logging.getLogger(__name__)


def part_blob_name(audio_blob_path: str, part_index: int) -> str:
    """The blob a part is stored in: the job's own WAV name (``uploads/{user_id}/{upload_id}-{stem}.wav``,
    which the upload request made and the ownership check trusts) with ``.part{NN}`` before the
    extension. Derived from what the server stored, never supplied by a client; under
    ``uploads/{user_id}/`` as the ownership check requires, and its upload ID is the upload's."""
    stem = audio_blob_path.removesuffix(".wav")
    return f"{stem}.part{part_index:02d}.wav"


async def submit_part_to_speech(
    *,
    job_id: UUID,
    audio_url: str,
    part_index: int,
    part_count: int,
    locale: str,
    enable_diarization: bool,
) -> tuple[str, str]:
    """Submit one part to Azure Speech. Returns (Azure job ID, Azure job URL). Raises
    BatchSubmissionError or SpeechAuthError if Azure refuses it or cannot be authenticated to; the
    stage records the failure with the same codes as a job submitted whole."""
    with stage(
        "speech_submission", attributes={"job.part_index": part_index, "job.part_count": part_count}
    ) as submission:
        try:
            job_url = await submit_batch_job(
                audio_sas_url=audio_url,
                display_name=f"batch-{job_id}-part-{part_index}",
                locale=locale,
                enable_diarization=enable_diarization,
            )
        except BatchSubmissionError:
            submission.error("speech.submission_rejected")
            raise
        except SpeechAuthError:
            submission.error("speech.auth_failed")
            raise
        batch_job_id = batch_job_id_from_url(job_url)
        submission.set_dependency_ref(batch_job_id)
        return batch_job_id, job_url


async def _release(
    refs: list[jobs.PartRefs], audio_blob_path: str | None, storage: AudioStorage, job_id: UUID
) -> tuple[list[UUID], bool]:
    """Delete the Azure job and the blob each part still records, each step on its own. Returns the
    parts whose resources are all deleted and whether that is every one. A part with an Azure job
    recorded always has its blob deleted by its derived name too: the worker that made the Azure job
    may have stored the blob after its row was released (blob names are derived, never supplied)."""
    released: list[UUID] = []
    clean = True
    for ref in refs:
        ok = True
        if ref.batch_job_url:
            try:
                await delete_batch_job(ref.batch_job_url)
            except Exception as exc:
                ok = False
                logger.warning("Could not delete a part's batch job of job %s (%s)", job_id, type(exc).__name__)
        blob = ref.blob_path or (
            part_blob_name(audio_blob_path, ref.part_index) if ref.batch_job_url and audio_blob_path else None
        )
        if blob:
            try:
                await storage.delete(blob)
            except Exception as exc:
                ok = False
                logger.warning("Could not delete a part's blob of job %s (%s)", job_id, type(exc).__name__)
        if ok:
            released.append(ref.part_id)
        clean = clean and ok
    return released, clean


async def cleanup_job_parts(job_id: UUID, *, storage: AudioStorage | None = None, engine=None) -> bool:
    """Delete the part blobs and the Azure jobs of a job's parts, each best effort. Returns whether
    everything that was recorded could be deleted (False: something is left behind; the job is marked
    ``needs_cleanup`` and the parts keep what they recorded, so a later attempt can delete it). The
    rows stay as the record of the parts, with the blob and Azure URLs forgotten (the rows are the only
    record of what is left to delete, so they are never removed here). Deleting a job is
    ``delete_job_and_parts``."""
    storage = storage or get_audio_storage()
    engine = engine if engine is not None else get_engine()

    def read() -> tuple[list[jobs.PartRefs], str | None]:
        with Session(engine) as session:
            job = jobs.get_job_by_id(session, job_id)
            return jobs.get_part_refs(session, job_id), (job.audio_blob_path if job else None)

    refs, audio_blob_path = await asyncio.to_thread(read)
    released, clean = await _release(refs, audio_blob_path, storage, job_id)

    def settle() -> None:
        with Session(engine) as session:
            jobs.release_part_refs(session, released)
            if clean:
                jobs.clear_needs_cleanup(session, job_id)
            else:
                jobs.mark_needs_cleanup(session, job_id, "A part's blob or Azure job could not be deleted")

    if refs:
        await asyncio.to_thread(settle)
    return clean


# How often the last step of a deletion is tried again when the worker keeps recording parts.
DELETE_ATTEMPTS = 3


async def delete_job_and_parts(job_id: UUID, *, storage: AudioStorage | None = None, engine=None) -> bool:
    """Delete a job row and its parts, without losing a blob or an Azure job on the way (DIAAT-408).

    Every part's resources are deleted first (``cleanup_job_parts``). The job and its part rows then go
    in one transaction that locks the job, deletes the rows and checks what they recorded
    (``delete_job_unless_parts_unreleased``): a part the worker created or recorded after the resources
    were released is seen, its resources are deleted, and the last step is tried again, ``DELETE_ATTEMPTS``
    times at most. Returns False if a resource could not be deleted or the worker kept recording (the
    job is kept, nothing is lost: delete it again); True once the job is gone."""
    storage = storage or get_audio_storage()
    engine = engine if engine is not None else get_engine()
    if not await cleanup_job_parts(job_id, storage=storage, engine=engine):
        return False

    def last_step() -> tuple[list[jobs.PartRefs], str | None]:
        with Session(engine) as session:
            job = jobs.get_job_by_id(session, job_id)
            audio_blob_path = job.audio_blob_path if job else None
            return jobs.delete_job_unless_parts_unreleased(session, job_id), audio_blob_path

    for _ in range(DELETE_ATTEMPTS):
        unreleased, audio_blob_path = await asyncio.to_thread(last_step)
        if not unreleased:
            return True
        released, clean = await _release(unreleased, audio_blob_path, storage, job_id)

        def forget(released=released) -> None:
            with Session(engine) as session:
                jobs.release_part_refs(session, released)

        await asyncio.to_thread(forget)
        if not clean:
            await asyncio.to_thread(_mark, engine, job_id)
            return False
    return False


def _mark(engine, job_id: UUID) -> None:
    with Session(engine) as session:
        jobs.mark_needs_cleanup(session, job_id, "A part's blob or Azure job could not be deleted")
