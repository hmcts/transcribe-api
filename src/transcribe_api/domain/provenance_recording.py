"""The technical configuration that produced a transcript (DIAAT-286 AI provenance).

Stored with the job (``transcription_job.provenance``, JSONB), written in the same
transaction as the transcript (``save_job_results``), and never sent to monitoring
telemetry (decision OD7). Configuration only: no transcript text, file names, case
references or user identifiers.

* speech: the Azure Speech resource (endpoint host, region), the REST API version,
  the locale, diarization, the model Azure used (GUID and display name), the Azure
  batch job ID, and the submission options that shape the output;
* app_release: the release that saved the results (runtime/version.APP_VERSION, as
  /api/health and every telemetry record);
* generated_at: when the results were saved;
* post_processing: the American-to-British spelling conversion (uwotm8, whose
  package version is the dictionary version; there is no custom vocabulary or phrase
  list) and the speaker-processing steps applied, or the one that failed
  (transcript.postprocess_degraded, DIAAT-283).

Not exposed by the API: there is no administrator view, and the endpoint host and
Azure job ID are infrastructure details the browser has no use for. Access is direct
database access, pending the security and data decision on audit access (AC3).

Known limits: the submission options and app_release are the ones in force when the
results were saved, which differ from those at submission only if a release changed
them while the job was running (the job does not record its submitting release).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from transcribe_api.runtime.version import APP_VERSION
from transcribe_api.stt import batch_client
from transcribe_api.stt.speakers_recording import SpeakerStepReport
from transcribe_api.stt.speech_telemetry import (
    SPEECH_API_VERSION,
    model_ref,
    speech_region,
)
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import batch_job_id_from_url

logger = logging.getLogger(__name__)

PROVENANCE_SCHEMA_VERSION = 1
SPELLING_DICTIONARY_PACKAGE = "uwotm8"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpeechProvenance(_Model):
    service: Literal["azure-speech-batch-transcription"] = "azure-speech-batch-transcription"
    endpoint_host: str | None
    region: str | None
    api_version: str
    locale: str | None
    diarization: bool | None
    diarization_max_speakers: int | None
    model_ref: str | None
    model_display_name: str | None
    azure_job_id: str | None
    profanity_filter_mode: str
    punctuation_mode: str
    word_level_timestamps: bool


class PostProcessingProvenance(_Model):
    spelling_conversion: Literal["uwotm8"] = "uwotm8"
    spelling_dictionary_version: str | None
    speaker_steps_applied: list[str]
    speaker_step_degraded: str | None


class TranscriptProvenance(_Model):
    schema_version: int = PROVENANCE_SCHEMA_VERSION
    app_release: str
    # Set by save_job_results, in the transaction that saves the transcript.
    generated_at: datetime | None = None
    speech: SpeechProvenance
    post_processing: PostProcessingProvenance


def spelling_dictionary_version() -> str | None:
    try:
        return version(SPELLING_DICTIONARY_PACKAGE)
    except PackageNotFoundError:
        return None


def _endpoint_host() -> str | None:
    try:
        return urlparse(get_settings().AZURE_SPEECH_ENDPOINT).hostname or None
    except ValueError:
        return None


def build_provenance(
    *,
    locale: str | None,
    diarization: bool | None,
    status_data: Mapping[str, Any],
    batch_job_url: str | None,
    model_display_name: str | None,
    speaker_steps: SpeakerStepReport,
) -> TranscriptProvenance | None:
    """The job's provenance, or None if it cannot be built. Never raises.

    A transcript is never failed for want of its provenance: the cause (type only) is
    logged and the job is saved without it.
    """
    try:
        return TranscriptProvenance(
            app_release=APP_VERSION,
            speech=SpeechProvenance(
                endpoint_host=_endpoint_host(),
                region=speech_region(),
                api_version=SPEECH_API_VERSION,
                locale=locale,
                diarization=diarization,
                diarization_max_speakers=(batch_client.MAX_DIARIZATION_SPEAKERS if diarization else None),
                model_ref=model_ref(status_data),
                model_display_name=model_display_name,
                azure_job_id=batch_job_id_from_url(batch_job_url),
                profanity_filter_mode=batch_client.PROFANITY_FILTER_MODE,
                punctuation_mode=batch_client.PUNCTUATION_MODE,
                word_level_timestamps=batch_client.WORD_LEVEL_TIMESTAMPS,
            ),
            post_processing=PostProcessingProvenance(
                spelling_dictionary_version=spelling_dictionary_version(),
                speaker_steps_applied=speaker_steps.applied,
                speaker_step_degraded=speaker_steps.degraded,
            ),
        )
    except Exception as exc:
        logger.warning("Could not build the transcript provenance (%s)", type(exc).__name__)
        return None
