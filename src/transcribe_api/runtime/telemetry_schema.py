"""The merged telemetry schema for transcribe-api (DIAAT-205).

Every app attribute and event, dictation and recording alike, is registered here,
once. hmcts-otel raises ``ValueError`` when a name is registered twice with a
different definition (descriptions included), and an event registered with an
attribute list drops any attribute not on it, so the two heritage services' schemas
could not simply both be imported into one process. Service modules import names
from here and never call ``default_schema.attribute`` / ``.event`` themselves.

The merge follows the design note (courtstranscribe ``docs/observability/
cnp-telemetry-schema.md``):

* attributes both services defined differently take the union of their values
  (``security.action``, ``security.resource_type``) or the looser check
  (``speech.error_code``: pattern only) and one description;
* ``audit.action`` is one vocabulary: the auth library's ``ACCESS_DENIED`` and
  ``ROLE_CHANGE``, then app actions as ``<object>.<verb>`` (decision 2);
* ``job.finished`` and ``transcript.available``, emitted by both, list the union of
  their attributes;
* the browser's ``upload.validation_failed`` is renamed ``upload.file_rejected``;
  ``upload.validation_failed`` is the recording API's file checks (decision 3).

Not here yet: ``hmcts.area`` (decision 1), registered with the code that sets it,
and dictation's stage events, registered with the code that emits them (api-4):
registered now, without their attribute lists, they would drop those attributes.

``telemetry-contract.json`` at the repository root is this schema as JSON, for
transcribe-web and the shared infrastructure's query checks; a unit test fails when
it is stale.
"""

from __future__ import annotations

import json
from typing import Any

from hmcts_otel import EventClass, TelemetrySchema, default_schema

# hmcts_azure_auth writes these to audit_log.event_type itself; they cannot change.
AUDIT_ACTIONS_AUTH_LIBRARY = ("ACCESS_DENIED", "ROLE_CHANGE")
# Actions the app writes, as <object>.<verb>. Dictation's TRANSCRIPT_VIEWED,
# TRANSCRIPT_EXPORTED, TRANSCRIPT_DELETED and USER_DELETED became transcript.view,
# transcript.export, transcript.delete and user.delete.
AUDIT_ACTIONS_APP = (
    "transcript.view",
    "transcript.export",
    "transcript.edit",
    "transcript.delete",
    "job.submit",
    "job.delete",
    "audio.export",
    "user.delete",
)
AUDIT_ACTIONS = AUDIT_ACTIONS_AUTH_LIBRARY + AUDIT_ACTIONS_APP


def register_schema(schema: TelemetrySchema = default_schema) -> TelemetrySchema:
    """Register every app attribute and event. Idempotent."""
    # Attributes first: an event may only list registered attributes.
    for register in (
        _shared_attributes,
        _dictation_attributes,
        _recording_attributes,
        _shared_events,
        _dictation_events,
        _recording_events,
        _browser_events,
    ):
        register(schema)
    return schema


def schema_contract(schema: TelemetrySchema) -> dict[str, Any]:
    """The schema as plain data: what telemetry-contract.json holds."""
    attributes = {
        name: {
            "type": spec.type.__name__,
            "allowed_values": sorted(spec.allowed_values) if spec.allowed_values is not None else None,
            "pattern": spec.pattern,
            "max_length": spec.max_length,
            "description": spec.description,
        }
        for name, spec in sorted(schema.attributes.items())
    }
    events = {
        name: {
            "class": spec.event_class.value,
            "attributes": list(spec.attributes),
            "browser": spec.browser,
            "description": spec.description,
        }
        for name, spec in sorted(schema.events.items())
    }
    return {"attributes": attributes, "events": events}


def render_contract(schema: TelemetrySchema) -> str:
    return json.dumps(schema_contract(schema), indent=2, sort_keys=True) + "\n"


def _shared_attributes(schema: TelemetrySchema) -> None:
    """Registered by both heritage services; one definition each (design note, section 2)."""
    schema.attribute(
        "audit.action",
        allowed_values=AUDIT_ACTIONS,
        description="Audited action: an hmcts_azure_auth value (ACCESS_DENIED, ROLE_CHANGE) or an app action, <object>.<verb>.",
    )
    schema.attribute(
        "dependency.name",
        allowed_values=("database", "speech", "storage"),
        description="A critical dependency: database (PostgreSQL), storage (Azure Blob Storage) or speech (Azure Speech batch transcription).",
    )
    schema.attribute(
        "dependency.state",
        allowed_values=("down", "up"),
        description="up: the last health check succeeded; down: it failed or timed out.",
    )
    schema.attribute("file.size_bytes", int, description="Size of the uploaded file in bytes.")
    schema.attribute("http.response.status_code", int, description="HTTP status of the failed Azure Speech call.")
    schema.attribute(
        "security.action",
        allowed_values=("delete", "read", "reassign", "submit", "write"),
        description="What the caller tried to do with the resource: read, write, delete, submit (recording) or reassign (dictation).",
    )
    schema.attribute(
        "security.outcome",
        allowed_values=("blocked", "detected", "test"),
        description="blocked: refused by the authorisation check; detected: let through by authorisation but caught by the post-authorisation ownership assertion (a defect); test: a controlled test of the alert route.",
    )
    schema.attribute(
        "security.resource_type",
        allowed_values=(
            "audio",
            "document",
            "draft",
            "job",
            "minute_version",
            "transcript",
            "transcription",
            "transcription_job",
            "upload",
        ),
        description="Kind of resource the caller does not own. Dictation: transcription, transcription_job, document, audio, draft, minute_version. Recording: job, transcript, audio, upload.",
    )
    schema.attribute(
        "speech.error_code",
        pattern="[A-Za-z][A-Za-z0-9_.]{0,63}",
        description="Azure Speech error code: a Speech SDK CancellationErrorCode name (browser) or a batch REST API code.",
    )


def _dictation_attributes(schema: TelemetrySchema) -> None:
    """Dictation service (heritage courtstranscribe)."""
    schema.attribute("dictation.autosave_failures", int, description="Failed autosaves in the dictation session.")
    schema.attribute(
        "dictation.collapses_in_window", int, description="Unexpected speech session ends in the health window."
    )
    schema.attribute("dictation.elapsed_seconds", int, description="Seconds recording ran before the warning.")
    schema.attribute(
        "dictation.end_reason",
        allowed_values=("left_page", "page_hidden", "submitted"),
        description="How the dictation session ended.",
    )
    schema.attribute("dictation.mode", allowed_values=("mic", "screen", "upload"), description="Recording mode.")
    schema.attribute(
        "dictation.reconnect_attempt",
        int,
        description="Reconnect attempt that started this speech session (0 for a user start).",
    )
    schema.attribute("dictation.segments_captured", int, description="Transcript segments captured.")
    schema.attribute(
        "dictation.session_id",
        pattern=r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        description="Browser dictation session that made this submission (set by the server from X-Dictation-Session-Id).",
    )
    schema.attribute(
        "dictation.session_index",
        int,
        description="Speech session number within the dictation session (reconnects count).",
    )
    schema.attribute("dictation.speech_sessions", int, description="Speech sessions in the dictation session.")
    schema.attribute(
        "dictation.transcription_id",
        pattern="[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        description="Transcription created by a submission.",
    )
    schema.attribute("dictation.user_initiated", bool, description="The user asked the session to end.")
    schema.attribute("dictation.window_seconds", int, description="Health window length.")
    schema.attribute("document.template", allowed_values=("decision",), description="Document template.")
    schema.attribute(
        "document.template_version",
        pattern="[0-9a-f]{12}|unknown",
        description="Template version (first 12 hex characters of its SHA-256).",
    )
    schema.attribute(
        "file.type", pattern="[a-z]+/[a-z0-9.+-]{1,60}|unknown", description="MIME type, without parameters."
    )
    schema.attribute("live_draft.consecutive_failures", int, description="Consecutive failed autosaves.")
    schema.attribute(
        "live_draft.failure_kind", allowed_values=("auth", "network", "server"), description="Why an autosave failed."
    )
    schema.attribute("live_draft.outage_seconds", int, description="Length of the autosave outage that ended.")
    schema.attribute(
        "live_draft.seconds_since_last_save", int, description="Seconds since the last successful autosave."
    )
    schema.attribute(
        "live_draft.trigger",
        allowed_values=("interval", "late-finalisation", "pause", "retry", "unmount"),
        description="What triggered the autosave.",
    )
    schema.attribute("minutes.citation_index", int, description="Which citation was clicked.")
    schema.attribute("minutes.content_length", int, description="Characters copied.")
    schema.attribute("minutes.rating", int, description="Minutes rating (1-5).")
    schema.attribute("minutes.template", allowed_values=("Crissa", "General"), description="Minutes template.")
    schema.attribute("onboarding.step", int, description="Onboarding step number.")
    schema.attribute(
        "onboarding.step_name",
        allowed_values=("ready", "review_edit", "transcribe", "unknown", "welcome"),
        description="Onboarding step.",
    )
    schema.attribute("page.route", pattern="/[a-z0-9/_:.\\-]{0,100}", description="Page route, IDs replaced by :id.")
    schema.attribute("recording.duration_seconds", int, description="Recording length.")
    schema.attribute("recording.mode", allowed_values=("mic", "screen"), description="Recording kind.")
    schema.attribute("speakers.segment_seconds", float, description="Length of the speaker sample played.")
    schema.attribute(
        "speech.cancel_reason",
        allowed_values=("CancelledByUser", "EndOfStream", "Error", "StartFailed", "Unknown"),
        description="Speech SDK CancellationReason.",
    )
    schema.attribute("speech.session_id", pattern="[0-9a-fA-F]{32}", description="Azure Speech SDK session ID.")
    schema.attribute(
        "speech.stop_reason",
        allowed_values=("collapsed", "page_exit", "paused", "submitted", "teardown"),
        description="Why the Speech session stopped.",
    )
    schema.attribute(
        "transcript.update_type",
        allowed_values=("all_occurrences", "single_occurrence"),
        description="Speaker rename scope.",
    )
    schema.attribute("upload.method", allowed_values=("direct", "proxy"), description="How the browser uploads audio.")
    schema.attribute("upload.retry_attempt", int, description="Upload attempt number.")
    schema.attribute(
        "upload.source",
        allowed_values=("backup_retry", "recording", "upload"),
        description="Where the audio came from.",
    )


def _recording_attributes(schema: TelemetrySchema) -> None:
    """Recording service (heritage batch-audio-transcription)."""
    schema.attribute(
        "audio.duration_seconds", float, description="Duration of the normalised (16 kHz mono WAV) audio, in seconds."
    )
    schema.attribute(
        "audio.format",
        allowed_values=("flac", "m4a", "mp3", "mp4", "ogg", "wav"),
        description="Extension of the uploaded audio file.",
    )
    schema.attribute(
        "audio.format_category",
        allowed_values=("audio", "video"),
        description="Whether the upload was an audio or a video container (.mp4).",
    )
    schema.attribute(
        "hmcts.failure.source",
        allowed_values=("application", "dependency", "queue"),
        description="Where a failure came from: application (our code or database), queue (the job was never processed) or dependency (Azure Speech).",
    )
    schema.attribute(
        "job.processing_ms", float, description="Processing: Azure Speech processing started to finished, in ms."
    )
    schema.attribute(
        "job.queue_wait_ms", float, description="Queue wait: job created to Azure Speech processing started, in ms."
    )
    schema.attribute("job.retry.attempt", int, description="Number of the attempt that failed (1 on the first retry).")
    schema.attribute(
        "job.retry.operation",
        allowed_values=("speech.delete", "speech.results", "speech.status", "speech.submit", "webhook.deliver"),
        description="The call being retried.",
    )
    schema.attribute(
        "job.state",
        allowed_values=("running", "waiting"),
        description="waiting: not yet processed by Azure Speech; running: being processed.",
    )
    schema.attribute(
        "speech.api_version",
        pattern="v[0-9]{1,2}\\.[0-9]{1,2}(-preview(\\.[0-9]{1,2})?)?",
        description="Speech to text REST API version, e.g. v3.2.",
    )
    schema.attribute(
        "speech.audio_duration_s",
        float,
        description="Seconds of audio recorded as Azure Speech usage for the job (OBS-5).",
    )
    schema.attribute(
        "speech.audio_duration_source",
        allowed_values=("azure", "estimate"),
        description="Where speech.audio_duration_s came from: azure (Azure's results file) or estimate (the duration given when the job was submitted).",
    )
    schema.attribute("speech.diarization", bool, description="Whether speaker diarization was on.")
    schema.attribute(
        "speech.locale", pattern="[a-z]{2,3}-[A-Z]{2}", description="Locale the job was transcribed in, e.g. en-GB."
    )
    schema.attribute(
        "speech.model_ref",
        pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        description="ID (GUID) of the Speech model Azure used for the job.",
    )
    schema.attribute(
        "speech.region", pattern="[a-z0-9]{2,32}", description="Azure region of the Speech resource, e.g. uksouth."
    )
    schema.attribute(
        "speech.retry_after_s",
        float,
        description="Retry-After sent by Azure Speech with a throttled (429) response, in s.",
    )
    schema.attribute(
        "storage.operation",
        allowed_values=("blob.mark_failed", "job.mark_failed", "job.mark_needs_cleanup", "transcript.save"),
        description="The database or blob write that failed.",
    )
    schema.attribute(
        "transcript.postprocess_step",
        allowed_values=("speaker_grouping", "speaker_labelling", "speaker_normalisation"),
        description="The speaker-processing step that failed.",
    )
    schema.attribute("transcript.segment_count", int, description="Number of dialogue entries in the transcript.")
    schema.attribute("transcript.speaker_count", int, description="Number of distinct speakers in the transcript.")
    schema.attribute(
        "upload.file_count", int, description="Number of files uploaded for the hearing (X-Hearing-File-Count)."
    )
    schema.attribute(
        "webhook.notification",
        allowed_values=("failed", "succeeded"),
        description="Which result the webhook carried: the job succeeded or failed.",
    )


def _shared_events(schema: TelemetrySchema) -> None:
    """Registered by both heritage services; one definition each (design note, section 2)."""
    schema.event(
        "audit.write_failed",
        attributes=("audit.action", "hmcts.error.code"),
        description="An audit record could not be written to the database; the request carried on (fail-open). hmcts.error.code: audit.write_failed. Correlate on hmcts.request.id. Never carries the actor, the job or any content.",
    )
    schema.event(
        "dependency.health_changed",
        attributes=("dependency.name", "dependency.state", "hmcts.error.code"),
        description="A dependency went from up to down or back (or was down on its first check). hmcts.error.code (when down): dependency.timeout, dependency.auth_failed, dependency.unreachable or dependency.error.",
    )
    schema.event(
        "job.finished",
        attributes=(
            "job.queue_wait_ms",
            "job.processing_ms",
            "hmcts.failure.source",
            "speech.error_code",
            "http.response.status_code",
            "speech.retry_after_s",
            "speech.region",
            "speech.locale",
            "speech.api_version",
            "speech.model_ref",
            "speech.diarization",
            "speech.audio_duration_s",
            "speech.audio_duration_source",
        ),
        description="A job finished, once per job: hmcts.outcome success, error (counts towards the failed-jobs alert), failure (the user's input was rejected) or cancelled; hmcts.error.code says why. Dictation: an uploaded-audio transcription. Recording: a batch Speech job, with queue and processing times and the Speech details.",
    )
    schema.event(
        "security.ownership_violation",
        attributes=("security.resource_type", "security.action", "security.outcome"),
        description="A caller reached, or tried to reach, a resource they do not own. Correlate on hmcts.request.id; hmcts.journey.id is the target job or upload.",
    )
    schema.event(
        "transcript.available",
        attributes=("transcript.segment_count", "transcript.speaker_count", "dictation.transcription_id"),
        description="A transcript was saved and can be opened. Dictation: dictation.transcription_id is the transcription created (live submission). Recording: segment and speaker counts.",
    )


def _dictation_events(schema: TelemetrySchema) -> None:
    """Dictation service (heritage courtstranscribe)."""
    schema.event(
        "document.available",
        attributes=("document.template", "document.template_version"),
        description="A generated document is ready to download; which template and version produced it.",
    )
    schema.event(
        "upload.rejected",
        description="The dictation API refused an upload before storing it: the key was invalid (upload.invalid_key) or not the caller's (upload.forbidden). Not upload.validation_failed (recording file checks) or upload.file_rejected (browser).",
    )
    schema.event(
        "upload.url_issued",
        attributes=("upload.method",),
        description="The API issued an upload target to the browser (direct to storage, or through the API).",
    )


def _recording_events(schema: TelemetrySchema) -> None:
    """Recording service (heritage batch-audio-transcription)."""
    schema.event("job.created", description="Job row created: the upload is complete and the job exists.")
    schema.event(
        "job.poll_failed",
        attributes=("hmcts.failure.source", "speech.error_code", "http.response.status_code", "speech.retry_after_s"),
        description="Polling the job failed; it stays unfinished and is polled again.",
    )
    schema.event(
        "job.queued",
        attributes=("speech.region", "speech.locale", "speech.api_version", "speech.model_ref", "speech.diarization"),
        description="Azure Speech accepted the job; duration_ms: created to accepted.",
    )
    schema.event(
        "job.retried",
        attributes=(
            "job.retry.operation",
            "job.retry.attempt",
            "hmcts.failure.source",
            "speech.error_code",
            "http.response.status_code",
            "speech.retry_after_s",
        ),
        description="A call for the job failed and is retried; hmcts.error.code says why.",
    )
    schema.event(
        "job.storage_failed",
        attributes=("storage.operation", "hmcts.failure.source"),
        description="A database or blob write recording a job's state failed; hmcts.error.code says which kind (storage.db_* or storage.blob_failed).",
    )
    schema.event(
        "job.stuck",
        attributes=("job.state", "hmcts.failure.source"),
        description="The job has waited or run longer than its threshold. Emitted once per job; duration_ms: time in that state so far.",
    )
    schema.event(
        "results_retrieval.cancelled", description="Downloading the transcript from Azure Speech was cancelled."
    )
    schema.event(
        "results_retrieval.completed",
        description="Downloading the transcript from Azure Speech completed; duration_ms: its duration.",
    )
    schema.event(
        "results_retrieval.failed",
        description="Downloading the transcript from Azure Speech failed; hmcts.error.code says why, duration_ms: its duration.",
    )
    schema.event("results_retrieval.started", description="Downloading the transcript from Azure Speech started.")
    schema.event(
        "speech.job_failed",
        attributes=("hmcts.failure.source", "speech.error_code"),
        description="Azure Speech reported the job as failed; speech.error_code is Azure's code.",
    )
    schema.event("speech.job_running", description="Azure Speech started processing; duration_ms: the queue wait.")
    schema.event(
        "transcript.postprocess_degraded",
        attributes=("transcript.postprocess_step",),
        description="A speaker-processing step failed; the previous step's output is kept.",
    )
    schema.event("transcript_persistence.cancelled", description="Saving the transcript to the database was cancelled.")
    schema.event(
        "transcript_persistence.completed",
        description="Saving the transcript to the database completed; duration_ms: its duration.",
    )
    schema.event(
        "transcript_persistence.failed",
        description="Saving the transcript to the database failed; hmcts.error.code says why, duration_ms: its duration.",
    )
    schema.event("transcript_persistence.started", description="Saving the transcript to the database started.")
    schema.event("transcript_postprocessing.cancelled", description="Speaker processing was cancelled.")
    schema.event(
        "transcript_postprocessing.completed", description="Speaker processing completed; duration_ms: its duration."
    )
    schema.event(
        "transcript_postprocessing.failed",
        description="Speaker processing failed; hmcts.error.code says why, duration_ms: its duration.",
    )
    schema.event("transcript_postprocessing.started", description="Speaker processing started.")
    schema.event(
        "upload.validation_failed",
        attributes=(
            "hmcts.stage",
            "hmcts.error.code",
            "upload.file_count",
            "file.size_bytes",
            "audio.format",
            "audio.format_category",
        ),
        description="An uploaded file was rejected; hmcts.error.code says why.",
    )
    schema.event(
        "webhook.delivered",
        attributes=("webhook.notification",),
        description="The caller's webhook was delivered (2xx).",
    )
    schema.event(
        "webhook.delivery_failed",
        attributes=("webhook.notification",),
        description="The caller's webhook could not be delivered after all attempts.",
    )


def _browser_events(schema: TelemetrySchema) -> None:
    """Browser events accepted by the telemetry relay (transcribe-web)."""
    schema.event(
        "auth.token_refresh_failed", browser=True, description="The browser could not obtain a fresh sign-in token."
    )
    schema.event(
        "dictation.no_speech_warning",
        attributes=("dictation.elapsed_seconds",),
        browser=True,
        description="Recording with no captured speech.",
    )
    schema.event("dictation.paused", browser=True, description="User paused recording.")
    schema.event(
        "dictation.resume_first_result",
        browser=True,
        description="First text after a resume (duration: resume to text).",
    )
    schema.event("dictation.resumed", browser=True, description="User resumed recording.")
    schema.event(
        "dictation.session_ended",
        attributes=(
            "hmcts.outcome",
            "hmcts.error.code",
            "dictation.end_reason",
            "dictation.segments_captured",
            "dictation.speech_sessions",
            "dictation.autosave_failures",
        ),
        browser=True,
        description="One per dictation session: its overall outcome (duration: session length).",
    )
    schema.event(
        "dictation.session_requested",
        attributes=("dictation.session_index", "dictation.reconnect_attempt"),
        browser=True,
        description="Recording requested (user start or reconnect).",
    )
    schema.event(
        "dictation.submission_finished",
        attributes=("hmcts.outcome", "hmcts.error.code", "dictation.mode", "http.response.status_code"),
        browser=True,
        description="Hearing submission finished (duration: request time).",
    )
    schema.event(
        "live_draft.autosave_stalled",
        attributes=("live_draft.failure_kind", "live_draft.consecutive_failures", "live_draft.seconds_since_last_save"),
        browser=True,
        description="Autosave has stopped working.",
    )
    schema.event(
        "live_draft.restore_finished",
        attributes=("hmcts.outcome", "hmcts.error.code", "live_draft.failure_kind", "http.response.status_code"),
        browser=True,
        description="Restoring a saved draft finished.",
    )
    schema.event(
        "live_draft.save_failed",
        attributes=(
            "live_draft.failure_kind",
            "http.response.status_code",
            "live_draft.consecutive_failures",
            "live_draft.seconds_since_last_save",
            "live_draft.trigger",
        ),
        browser=True,
        description="An autosave failed.",
    )
    schema.event(
        "live_draft.save_recovered",
        attributes=("live_draft.consecutive_failures", "live_draft.outage_seconds"),
        browser=True,
        description="Autosave works again after failures.",
    )
    schema.event(
        "minutes.ai_edit_completed",
        event_class=EventClass.ANALYTICS,
        attributes=("minutes.template",),
        browser=True,
        description="An AI edit finished (duration).",
    )
    schema.event(
        "minutes.ai_edit_failed",
        attributes=("minutes.template", "hmcts.outcome", "hmcts.error.code"),
        browser=True,
        description="An AI edit failed (duration).",
    )
    schema.event(
        "minutes.ai_edit_started",
        event_class=EventClass.ANALYTICS,
        attributes=("minutes.template",),
        browser=True,
        description="An AI edit started.",
    )
    schema.event(
        "minutes.citation_clicked",
        event_class=EventClass.ANALYTICS,
        attributes=("minutes.citation_index",),
        browser=True,
        description="A citation was clicked.",
    )
    schema.event(
        "minutes.copied",
        event_class=EventClass.ANALYTICS,
        attributes=("minutes.content_length",),
        browser=True,
        description="Minutes were copied.",
    )
    schema.event(
        "minutes.generation_completed",
        event_class=EventClass.ANALYTICS,
        attributes=("minutes.template",),
        browser=True,
        description="Minutes generated (duration).",
    )
    schema.event(
        "minutes.generation_failed",
        attributes=("minutes.template", "hmcts.outcome", "hmcts.error.code"),
        browser=True,
        description="Minutes generation failed (duration).",
    )
    schema.event(
        "minutes.generation_started",
        event_class=EventClass.ANALYTICS,
        attributes=("minutes.template",),
        browser=True,
        description="Minutes generation started.",
    )
    schema.event(
        "minutes.manual_edit_saved",
        event_class=EventClass.ANALYTICS,
        browser=True,
        description="Minutes were edited by hand.",
    )
    schema.event(
        "minutes.rated",
        event_class=EventClass.ANALYTICS,
        attributes=("minutes.rating",),
        browser=True,
        description="Minutes were rated.",
    )
    schema.event(
        "onboarding.completed", event_class=EventClass.ANALYTICS, browser=True, description="Onboarding was completed."
    )
    schema.event(
        "onboarding.licence_check_failed",
        attributes=("onboarding.step", "hmcts.error.code"),
        browser=True,
        description="The licence check during onboarding failed.",
    )
    schema.event(
        "onboarding.step_viewed",
        event_class=EventClass.ANALYTICS,
        attributes=("onboarding.step", "onboarding.step_name"),
        browser=True,
        description="An onboarding step was shown.",
    )
    schema.event(
        "page.viewed",
        event_class=EventClass.ANALYTICS,
        attributes=("page.route",),
        browser=True,
        description="A page was viewed.",
    )
    schema.event(
        "recording.completed",
        event_class=EventClass.ANALYTICS,
        attributes=("recording.mode", "recording.duration_seconds", "file.size_bytes", "file.type"),
        browser=True,
        description="A recording finished.",
    )
    schema.event(
        "recording.failed",
        attributes=("recording.mode", "recording.duration_seconds"),
        browser=True,
        description="A recording failed.",
    )
    schema.event(
        "recording.new_clicked",
        event_class=EventClass.ANALYTICS,
        attributes=("dictation.mode",),
        browser=True,
        description="Start new recording was clicked.",
    )
    schema.event(
        "recording.started",
        event_class=EventClass.ANALYTICS,
        attributes=("recording.mode", "file.type"),
        browser=True,
        description="A recording started.",
    )
    schema.event(
        "speakers.sample_played",
        event_class=EventClass.ANALYTICS,
        attributes=("speakers.segment_seconds",),
        browser=True,
        description="A speaker sample was played.",
    )
    schema.event(
        "speakers.sample_renamed",
        event_class=EventClass.ANALYTICS,
        browser=True,
        description="A speaker was renamed in the sample editor.",
    )
    schema.event(
        "speech.canceled",
        attributes=(
            "speech.session_id",
            "dictation.session_index",
            "speech.cancel_reason",
            "speech.error_code",
            "dictation.collapses_in_window",
        ),
        browser=True,
        description="Speech SDK canceled event.",
    )
    schema.event(
        "speech.first_result",
        attributes=("speech.session_id", "dictation.session_index"),
        browser=True,
        description="First transcript text (duration: request to first text).",
    )
    schema.event(
        "speech.session_started",
        attributes=("speech.session_id", "dictation.session_index", "dictation.reconnect_attempt"),
        browser=True,
        description="Speech SDK sessionStarted (duration: request to ready).",
    )
    schema.event(
        "speech.session_stopped",
        attributes=(
            "speech.session_id",
            "dictation.session_index",
            "dictation.segments_captured",
            "dictation.user_initiated",
            "speech.stop_reason",
        ),
        browser=True,
        description="Speech session ended (duration: session length).",
    )
    schema.event(
        "speech.token_fetched",
        attributes=("hmcts.outcome", "hmcts.error.code"),
        browser=True,
        description="Speech token request finished (duration: fetch time).",
    )
    schema.event(
        "speech.unhealthy",
        attributes=(
            "dictation.collapses_in_window",
            "dictation.window_seconds",
            "speech.cancel_reason",
            "speech.error_code",
        ),
        browser=True,
        description="Speech sessions collapsing repeatedly.",
    )
    schema.event(
        "support.error_report_copied",
        event_class=EventClass.ANALYTICS,
        attributes=("hmcts.request.id",),
        browser=True,
        description="An error report was copied.",
    )
    schema.event(
        "support.error_report_downloaded",
        event_class=EventClass.ANALYTICS,
        attributes=("hmcts.request.id",),
        browser=True,
        description="An error report was downloaded.",
    )
    schema.event(
        "transcript.deleted", event_class=EventClass.ANALYTICS, browser=True, description="A transcript was deleted."
    )
    schema.event(
        "transcript.marked_done",
        event_class=EventClass.ANALYTICS,
        browser=True,
        description="A transcript was marked done.",
    )
    schema.event(
        "transcript.opened", event_class=EventClass.ANALYTICS, browser=True, description="A transcript was opened."
    )
    schema.event(
        "transcript.renamed", event_class=EventClass.ANALYTICS, browser=True, description="A transcript was renamed."
    )
    schema.event(
        "transcript.speaker_renamed",
        event_class=EventClass.ANALYTICS,
        attributes=("transcript.update_type",),
        browser=True,
        description="A speaker was renamed.",
    )
    schema.event(
        "transcript.unmarked_done",
        event_class=EventClass.ANALYTICS,
        browser=True,
        description="A transcript was marked not done.",
    )
    schema.event(
        "upload.file_rejected",
        attributes=("hmcts.error.code", "file.type", "file.size_bytes"),
        browser=True,
        description="A file the user picked was rejected in the browser, before upload (count, size or type).",
    )
    schema.event(
        "upload.file_selected",
        event_class=EventClass.ANALYTICS,
        attributes=("file.type", "file.size_bytes"),
        browser=True,
        description="A file was chosen for upload.",
    )
    schema.event(
        "upload.transcription_started",
        event_class=EventClass.ANALYTICS,
        attributes=("upload.source", "file.type", "upload.retry_attempt"),
        browser=True,
        description="An upload was sent for transcription.",
    )
