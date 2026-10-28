"""Telemetry for transcribe-api (DIAAT-205), built on hmcts-otel.

Call ``configure_telemetry()`` once per process, before the FastAPI app is created
or the pollers start.
"""

from __future__ import annotations

import logging
import os
import re

import contextlib
from contextlib import AbstractContextManager
from pathlib import PurePosixPath
from uuid import UUID

from hmcts_otel import Journey, TelemetrySettings, current_journey, default_redactor, journey, setup_telemetry

from transcribe_api.runtime.settings_dictation import get_settings
from transcribe_api.runtime.telemetry_schema import register_schema
from transcribe_api.runtime.version import APP_VERSION

SERVICE_NAME_API = "transcribe-api"
SERVICE_NAME_WORKER = "transcribe-api-worker"

# Every app attribute and event, at import: modules that emit events import this one,
# so the schema is in place before they emit, in tests (strict) as in the services.
register_schema()

# Journey types (DIAAT-276). Query one identifier in App Insights:
#   customEvents | where customDimensions["hmcts.journey.id"] == "<id>"
# Uploaded audio: the UUID in the uploaded file's name is also the transcription ID,
# so one ID runs from upload through transcription, minutes, document and email.
JOURNEY_TRANSCRIPTION = "dictation.transcription"
# Live dictation submission: id is the submission ID; the transcription created from
# it is related on the transcript.available event.
JOURNEY_SUBMISSION = "dictation.submission"
# Recording uploads: id is the UUID in the uploaded blob's name.
JOURNEY_UPLOAD = "recording.upload"
# Recording batch jobs: id is SpeechBatchJob.id; parent is the upload's id.
JOURNEY_JOB = "recording.job"

_UPLOAD_ID = re.compile(r"^uploads/[^/]+/([0-9a-fA-F-]{36})-")


def register_minutes_schema() -> None:
    """Register ``minutes.template``: now part of the merged schema. Idempotent."""
    register_schema()


def transcription_id_from_key(blob_key: str | None) -> str | None:
    """The transcription ID in ``user-uploads/{user}/{uuid}.{ext}``, canonical form.

    The single parser for this convention: the upload, the poller and the database
    all use the same ID, so the journey joins up. Any form ``UUID()`` accepts
    (upper case, 32 hex, braces) is normalised to lower-case hyphenated.
    """
    if not blob_key:
        return None
    try:
        return str(UUID(PurePosixPath(blob_key).stem))
    except ValueError:
        return None


def transcription_journey(transcription_id: str | UUID | None) -> AbstractContextManager[Journey | None]:
    """The transcription journey, or no journey when the ID is unknown.

    Guards the call sites: ``journey(type, None)`` would stringify to "None" and merge
    unrelated jobs into one journey. The ID is normalised to the canonical UUID form,
    and re-entering the current journey is a no-op.
    """
    if transcription_id is None:
        return contextlib.nullcontext()
    try:
        journey_id = str(UUID(str(transcription_id)))  # one form for every caller
    except ValueError:
        journey_id = str(transcription_id)  # rejected by journey() as not an ID
    current = current_journey()
    if current is not None and current.type == JOURNEY_TRANSCRIPTION and current.id == journey_id:
        return contextlib.nullcontext(current)  # already in it (e.g. the poller opened it)
    return journey(JOURNEY_TRANSCRIPTION, journey_id)


def upload_id_from_blob(blob_name: str | None) -> str | None:
    """The recording upload's journey ID (the UUID in ``uploads/{user}/{uuid}-{stem}.wav``)."""
    match = _UPLOAD_ID.match(blob_name or "")
    return match.group(1) if match else None


def batch_job_id_from_url(batch_job_url: str | None) -> str | None:
    """Azure Speech batch job ID, related to our job ID as ``hmcts.dependency.ref``."""
    return batch_job_url.rstrip("/").rsplit("/", 1)[-1] if batch_job_url else None


# Immigration and Asylum Chamber appeal references, e.g. PA/50123/2024. Uploaded file
# names carry the same reference with underscores (PA_50123_2024.mp3). "_" is a word
# character, so \b would miss references next to other text in file names
# (hearing_PA_50123_2024_part1.mp3): use lookarounds. Case-insensitive.
APPEAL_REFERENCE = r"(?i)(?<![A-Za-z0-9])[A-Z]{2}[/_]\d{5}[/_]\d{4}(?![0-9])"

# Recording uploads are named uploads/{user id}/{upload id}-{sanitised original file
# name}.wav (routes_recording), and the original file name is usually a case
# reference. The Azure SDK exports the blob URL (its HTTP logging policy, and url.full
# on its client spans, of which hmcts-otel strips only the query), and our own blob
# logs print the blob name. Remove whatever follows "/{uuid}/{uuid}-", keeping the IDs
# so the upload stays traceable; plain and percent-encoded paths. No length cap (a cap
# would leak the tail of a long name): the name stops only at a path separator,
# query, quote or space. A name that is already just the marker is skipped, but a
# partly redacted one (after appeal_reference) is still removed whole, so this rule is
# registered first.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_NAME_END = r"[\s/?#\"'<>\\]"
UPLOAD_FILE_NAME = (
    rf"(?:(?<=/{_UUID}/{_UUID}-)|(?<=%2[Ff]{_UUID}%2[Ff]{_UUID}-))"
    rf"(?!\[REDACTED\](?:{_NAME_END}|$))[^\s/?#\"'<>\\]+"
)


def register_redaction_rules() -> None:
    """Add this service's redaction rules, upload file names first. Idempotent."""
    present = {rule.name for rule in default_redactor.rules}
    if "upload_file_name" not in present:
        default_redactor.add_rule("upload_file_name", UPLOAD_FILE_NAME)
    if "appeal_reference" not in present:
        default_redactor.add_rule("appeal_reference", APPEAL_REFERENCE)


# The Azure Monitor exporter logs every upload at INFO, as does the HTTP logging policy
# of the pipeline it uploads with. Exported, each of those records causes another
# upload, which logs again: a feedback loop. Named as well as "azure", so raising the
# SDK's verbosity for debugging cannot restart it. hmcts-otel 0.2.3 also keeps these
# loggers off the exporting handler whatever their level; this stays as a second guard
# and to keep the SDK's INFO lines out of stdout.
EXPORT_LOOP_LOGGERS = ("azure.monitor.opentelemetry.exporter", "azure.core.pipeline.policies.http_logging_policy")


def quiet_azure_sdk_logging() -> None:
    """Keep the Azure SDK's INFO logging out of stdout and the exported logs."""
    for name in ("azure", *EXPORT_LOOP_LOGGERS):
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_telemetry(service_name: str = SERVICE_NAME_API) -> TelemetrySettings:
    """Set up logging, tracing and metrics for this process. Idempotent.

    ``service_name`` is the default for this process; ``OTEL_SERVICE_NAME`` wins.
    """
    register_redaction_rules()
    register_schema()
    quiet_azure_sdk_logging()  # before setup_telemetry creates the exporters
    overrides: dict[str, object] = {
        "service_version": APP_VERSION,
        "environment": get_settings().ENVIRONMENT,
        # No Sentry on CNP (HMCTS has no Sentry organisation): never initialise it,
        # whatever SENTRY_DSN says.
        "sentry_dsn": None,
    }
    if not any(os.environ.get(name) for name in ("OTEL_SERVICE_NAME", "HMCTS_OTEL_SERVICE_NAME")):
        overrides["service_name"] = service_name
    return setup_telemetry(TelemetrySettings(**overrides))
