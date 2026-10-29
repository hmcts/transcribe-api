import asyncio
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from docx import Document as DocxDocument
from fastapi import HTTPException
from hmcts_otel import Outcome, StageHandle, background_span, client_failure_code, record_event, stage
from hmcts_otel import conventions as c

from transcribe_api.documents.minutes.llm_calls import (
    generate_llm_output_task,
    generate_meeting_title,
)
from transcribe_api.documents.minutes.templates.templates_metadata import (
    crissa_template,
    general_template,
)
from transcribe_api.domain.exceptions import OwnershipConflictError
from transcribe_api.domain.interface_dictation import (
    get_transcription_title,
    load_or_create_transcription,
    save_transcription_job,
    save_transcription_title,
    user_error_message,
)
from transcribe_api.domain.models_dictation import (
    Transcription,
    TranscriptionJob,
)
from transcribe_api.domain.notify.gov_notify import send_email
from transcribe_api.domain.provenance_dictation import transcript_provenance
from transcribe_api.runtime.blob_dictation import AsyncAzureBlobManager
from transcribe_api.runtime.logger import logger
from transcribe_api.runtime.telemetry import transcription_journey
from transcribe_api.stt.fast_client import NoSpeechDetectedError, speech_configuration, transcribe_audio
from transcribe_api.stt.speakers_dictation import process_speakers_and_dialogue_entries
from transcribe_api.stt.utils import (
    get_file_blob_path,
    get_url_for_transcription,
)


async def generate_and_save_meeting_title(
    dialogue_entries: list, transcription: Transcription, user_id: UUID
) -> Transcription:
    """Generate and save the title; on failure the transcription is returned as stored."""
    await _generate_and_save_title(dialogue_entries, transcription, user_id)
    return transcription


async def _generate_and_save_title(dialogue_entries: list, transcription: Transcription, user_id: UUID) -> bool:
    """True when a title was generated and saved. The job's outcome uses this, so it
    always agrees with the title_generation stage."""
    with stage("title_generation") as title_stage:
        previous_title = transcription.title
        provisional_title = None
        try:
            logger.info(f"Generating meeting title for {len(dialogue_entries)} dialogue entries")
            provisional_title = await generate_meeting_title(dialogue_entries, str(user_id))
            if not (provisional_title and provisional_title.strip()):
                title_stage.error("title.failed")  # an empty title is not a title
                return False
            # Only if the title is unchanged since the job started: the job's copy of
            # the row may be stale (the user may have renamed it meanwhile).
            if save_transcription_title(transcription.id, user_id, provisional_title, previous_title):
                # The pipeline reads the title from this object (document, email, outcome).
                transcription.title = provisional_title
                return True
            stored = _stored_title(transcription)
            if stored is not None:  # renamed (or cleared) meanwhile: the user's title wins
                transcription.title = stored
                return True
            title_stage.error("title.failed")  # the transcription no longer exists
            transcription.title = previous_title
            return False
        except Exception as e:
            if provisional_title and _stored_title(transcription) == provisional_title:
                # The save committed and only a later step (e.g. the refresh) failed.
                transcription.title = provisional_title
                return True
            # Not saved: keep the object as the database has it, so the job reports
            # title.failed and does not use a title that was never stored.
            transcription.title = previous_title
            title_stage.error("title.failed")
            # Type only: LLM and database errors can echo content.
            logger.error("Error generating or saving meeting title (%s)", type(e).__name__, exc_info=e)
            return False


def _stored_title(transcription: Transcription) -> str | None:
    try:
        return get_transcription_title(transcription.id)
    except Exception:
        return None  # cannot tell: treat as not saved


async def _generate_and_upload_transcript_docx(
    dialogue_entries: list,
    azure_user_id: str,
    title: str,
) -> str | None:
    """Generate a plain .docx transcript from dialogue entries and upload to blob storage.

    Returns the blob path on success, None on failure.
    """
    try:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        doc_filename = f"transcript-{timestamp}.docx"
        doc_blob_path = get_file_blob_path(azure_user_id, doc_filename)

        output_dir = Path(tempfile.gettempdir()) / "transcripts"
        output_dir.mkdir(parents=True, exist_ok=True)
        temp_path = output_dir / doc_filename

        doc = DocxDocument()
        doc.add_heading(title, level=0)

        for entry in dialogue_entries:
            speaker = entry.speaker if hasattr(entry, "speaker") else entry.get("speaker", "Unknown")
            text = entry.text if hasattr(entry, "text") else entry.get("text", "")
            para = doc.add_paragraph()
            run = para.add_run(f"{speaker}: ")
            run.bold = True
            para.add_run(text)

        doc.save(str(temp_path))

        async with AsyncAzureBlobManager() as blob_manager:
            success = await blob_manager.create_blob_from_file(temp_path, doc_blob_path)

        if success:
            logger.info("Uploaded transcript document to blob %s for user %s", doc_blob_path, azure_user_id)
            return doc_blob_path
        else:
            logger.warning("Failed to upload transcript document for user %s", azure_user_id)
            return None

    except Exception as e:
        logger.error("Error generating transcript document (%s)", type(e).__name__, exc_info=e)
        return None


@dataclass
class _JobProgress:
    """Where the job is, so a failure at any point ends with the right terminal event."""

    step: str = "job_record"
    # The stage running the current step, if any: the job's outcome is taken from it,
    # so a stage and job.finished always agree.
    current_stage: StageHandle | None = None
    # Parts that failed without stopping the job, most significant first when reported.
    degraded: list[str] = field(default_factory=list)


_DEGRADED_PRIORITY = ("minutes.failed", "document.not_generated", "title.failed", "notification.email_failed")


async def transcribe_and_generate_llm_output(
    user_upload_blob_storage_file_key: str,
    user_id: UUID,
    user_email: str,
    azure_user_id: str,
    transcription_id: str | None = None,
):
    """Transcribe an uploaded file and generate minutes, document and email.

    DIAAT-276: the job runs in the transcription's journey and always ends with one
    ``job.finished`` event: ``success``; ``error`` with ``<step>.failed`` if it stopped;
    ``error`` with the most significant degraded part (minutes, document, title, email)
    if it completed with failures; or ``cancelled``.
    """
    # Own trace for the whole background task (OpenTelemetry).
    with background_span("Transcribe and Generate LLM Output", tracer_name=__name__):
        progress = _JobProgress()
        try:
            # The file name's UUID is client input: another user's transcription is
            # refused (SEC-1, OwnershipConflictError); the caller's own is loaded, not
            # reset (a retry).
            transcription = load_or_create_transcription(transcription_id, user_id)
        except OwnershipConflictError:
            # The file name names another user's transcription (SEC-1): a rejected
            # request, not a service error (outcome failure, out of the error alerts).
            # Deliberately no journey: the only ID is the one the file name names,
            # which is that user's transcription, and a rejected request must not
            # write into their journey (as the upload route). The investigator's trail
            # is the ACCESS_DENIED audit row (actor and that ID) and the blocked
            # security.ownership_violation event.
            record_event("job.finished", {c.OUTCOME: "failure", c.ERROR_CODE: "job.ownership_conflict"})
            raise
        except Exception as e:
            # The enclosing background span records the exception's redacted stack.
            logger.error("Transcription record could not be loaded (%s)", type(e).__name__)
            # An error outcome (the upload-job-failures alert): in the journey of the
            # transcription the job was for, so it can be traced to its upload. The
            # ID is the one the save would have used (the file name's UUID, as the
            # upload's journey; canonical form, as transcription.id). Its owner is not
            # known (the lookup failed): if the name were another user's, this one
            # event joins their journey; it carries no content. A file name that is
            # not a UUID has no ID yet (transcription_id None): no journey.
            with transcription_journey(transcription_id):
                record_event("job.finished", {c.OUTCOME: "error", c.ERROR_CODE: "job_record.failed"})
            raise
        # The row exists, so the ID is known even for non-UUID file names.
        with transcription_journey(transcription.id):
            try:
                await _run_pipeline(
                    transcription, progress, user_upload_blob_storage_file_key, user_id, user_email, azure_user_id
                )
            except asyncio.CancelledError:
                record_event("job.finished", {c.OUTCOME: "cancelled", c.ERROR_CODE: "job.cancelled"})
                raise
            except Exception:
                # As recorded by the stage that stopped the job; a step without a stage
                # (a database write) is an error.
                handle = progress.current_stage
                failed = handle is not None and handle.outcome is Outcome.FAILURE
                outcome = "failure" if failed else "error"
                record_event("job.finished", {c.OUTCOME: outcome, c.ERROR_CODE: f"{progress.step}.failed"})
                raise
            if progress.degraded:
                worst = min(progress.degraded, key=_DEGRADED_PRIORITY.index)
                record_event("job.finished", {c.OUTCOME: "error", c.ERROR_CODE: worst})
            else:
                record_event("job.finished", {c.OUTCOME: "success"})


async def _generate_minutes_and_title(
    dialogue_entries: list, transcription: Transcription, user_id: UUID, progress: _JobProgress
) -> None:
    """Both minutes templates and the title, in parallel. Failures degrade the job."""
    general_task = generate_llm_output_task(dialogue_entries, transcription.id, general_template, str(user_id))
    title_task = _generate_and_save_title(dialogue_entries, transcription, user_id)
    crissa_task = generate_llm_output_task(dialogue_entries, transcription.id, crissa_template, str(user_id))

    # Wait for all three, including after a failure: the title and the other template
    # must finish before the document, the email and the outcome use them.
    results = await asyncio.gather(general_task, title_task, crissa_task, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result  # cancellation and the like are not task failures
    # The title step reports its own outcome (True when saved), as its stage does.
    if results[1] is not True:
        progress.degraded.append("title.failed")
    minutes_errors = [r for r in (results[0], results[2]) if isinstance(r, Exception)]
    if minutes_errors:
        # The job carries on; the failure is reported on job.finished.
        progress.degraded.append("minutes.failed")
        for error in minutes_errors:  # each task's stage already recorded its exception
            logger.error("Minutes generation failed (%s)", type(error).__name__)


async def _run_pipeline(
    transcription: Transcription,
    progress: _JobProgress,
    user_upload_blob_storage_file_key: str,
    user_id: UUID,
    user_email: str,
    azure_user_id: str,
) -> None:
    try:
        progress.step = "transcription"
        with stage("transcription") as progress.current_stage:
            try:
                speech = speech_configuration()  # DIAAT-286: what the request is sent with
                dialogue_entries = await transcribe_audio(user_upload_blob_storage_file_key)
                transcribed_at = datetime.now(UTC)
            except HTTPException as exc:
                # transcription.py reports Azure Speech errors as HTTPExceptions:
                # 422 when Azure rejected the audio or request (the user's file: a
                # handled failure, job outcome "failure"), 5xx for auth, quota,
                # throttling and outages (a dependency error that counts towards
                # error rates and the upload-job-failures alert). Audio Azure
                # transcribed but found no speech in is a failure with its own code.
                if isinstance(exc, NoSpeechDetectedError):
                    progress.current_stage.fail(exc.error_code)
                elif client_failure_code(exc) is not None:
                    progress.current_stage.fail("speech.audio_rejected")
                else:
                    progress.current_stage.error("speech.request_failed")
                raise
        progress.step = "speaker_naming"
        with stage("speaker_naming") as progress.current_stage:
            updated_dialogue_entries = await process_speakers_and_dialogue_entries(dialogue_entries, str(user_id))
        progress.step = "job_record"
        progress.current_stage = None
        saved_job = save_transcription_job(
            TranscriptionJob(
                transcription_id=transcription.id,
                dialogue_entries=updated_dialogue_entries,
                s3_audio_url=user_upload_blob_storage_file_key,
                # Stored with the transcript, in the same insert (DIAAT-286).
                provenance=transcript_provenance(speech=speech, generated_at=transcribed_at).to_record(),
            ),
            check_owner=False,  # a new job: the ID is generated by the server
        )
    except Exception as e:
        # Type only: the exception is recorded, with its redacted stack, by the step's
        # stage and by the job's background span it propagates to (one record each,
        # not a third from this log).
        logger.error("Transcription failed (%s)", type(e).__name__)
        try:
            save_transcription_job(
                TranscriptionJob(
                    transcription_id=transcription.id,
                    dialogue_entries=[],
                    s3_audio_url=user_upload_blob_storage_file_key,
                    error_message=user_error_message(e, "Transcription"),
                ),
                check_owner=False,  # a new job: the ID is generated by the server
            )
        except Exception as save_error:
            # Nothing else records this one, and the transcription's failure, not the
            # save's, is what the job reports and re-raises.
            logger.error(
                "The failed transcription job could not be saved (%s)", type(save_error).__name__, exc_info=save_error
            )
        raise
    record_event("transcript.available")

    progress.step = "minutes"
    await _generate_minutes_and_title(updated_dialogue_entries, transcription, user_id, progress)

    # Generate a .docx transcript document and update s3_audio_url so that
    # "Download Transcript" serves the document rather than the raw audio file.
    # The title step sets transcription.title (only when it was saved).
    progress.step = "document_generation"
    with stage("document_generation") as document_stage:
        doc_blob_path = await _generate_and_upload_transcript_docx(
            updated_dialogue_entries,
            azure_user_id,
            transcription.title or "Transcript",
        )
        if not doc_blob_path:
            document_stage.error("document.not_generated")
            progress.degraded.append("document.not_generated")
    if doc_blob_path:
        progress.step = "job_record"
        saved_job.s3_audio_url = doc_blob_path
        save_transcription_job(saved_job, check_owner=False)  # the server's own job

    progress.step = "notification"
    with stage("notification") as notification_stage:
        try:
            send_email(
                user_email,
                get_url_for_transcription(transcription.id),
                transcription.title or "",
            )
        except Exception as e:
            notification_stage.error("notification.email_failed")
            progress.degraded.append("notification.email_failed")
            logger.error("Error sending email (%s)", type(e).__name__, exc_info=e)
