"""Submitting the parts of a recording longer than 240 minutes, and cleaning them up (DIAAT-408).

Each part is its own Azure batch job (``submit_part_to_speech``). The worker's preparation
(stt/preparation_service.py) stores and submits the parts with bounded parallelism and records each
in ``transcription_job_part``; this module is the Azure and storage side of one part, with the same
telemetry as a job submitted by its request: a ``speech_submission`` stage per part, carrying
``job.part_index`` and ``job.part_count``, and the Azure job's ID as the dependency reference.

``cleanup_job_parts`` removes every part blob and Azure job of a job (best effort, each step on
its own): used when a job fails, and when a job is deleted while its parts are in flight.
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


async def cleanup_job_parts(
    job_id: UUID, *, storage: AudioStorage | None = None, engine=None, delete_rows: bool = False
) -> bool:
    """Delete the part blobs and the Azure jobs of a job's parts, each best effort. Returns whether
    everything that was recorded could be deleted (False: something is left behind; the job is marked
    ``needs_cleanup``). ``delete_rows`` also removes the part rows (a deleted job); otherwise they stay
    as the record of the parts, with the blob and Azure URLs forgotten."""
    storage = storage or get_audio_storage()
    engine = engine if engine is not None else get_engine()

    def read() -> list[tuple[str | None, str | None]]:
        with Session(engine) as session:
            if delete_rows:  # one statement: the rows go and what they recorded comes back
                return jobs.delete_job_parts(session, job_id)
            return [(p.blob_path, p.batch_job_url) for p in jobs.get_job_parts(session, job_id)]

    parts = await asyncio.to_thread(read)
    clean = True
    for blob_path, batch_job_url in parts:
        if batch_job_url:
            try:
                await delete_batch_job(batch_job_url)
            except Exception as exc:
                clean = False
                logger.warning("Could not delete a part's batch job of job %s (%s)", job_id, type(exc).__name__)
        if blob_path:
            try:
                await storage.delete(blob_path)
            except Exception as exc:
                clean = False
                logger.warning("Could not delete a part's blob of job %s (%s)", job_id, type(exc).__name__)

    def settle() -> None:
        with Session(engine) as session:
            if clean:
                jobs.clear_part_resources(session, job_id)
                jobs.clear_needs_cleanup(session, job_id)
            else:
                jobs.mark_needs_cleanup(session, job_id, "A part's blob or Azure job could not be deleted")

    if parts and not delete_rows:
        await asyncio.to_thread(settle)
    return clean
