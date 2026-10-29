"""AI provenance: the configuration that produced a generated output (DIAAT-286).

Stored with the output, in ``transcriptionjob.provenance`` (JSONB), in the same
insert as the output itself, not in monitoring telemetry (decision OD7). The job row
is the record that holds the output: the decision document's blob path for a live
dictation submission, the transcript (dialogue entries) for uploaded audio. It is
deleted with the transcription, like the output.

Configuration only, never content: no transcript text, title, names or file names.

* Decision document (live dictation): the template (``decision``), its version for
  this render (renderer and document content, documents/versioning.py), the
  application release and the generation time. No Speech fields: the dictated text
  comes from the browser's real-time Speech session, and the browser does not send
  its Speech settings with the submission.
* Transcript (uploaded audio): the Azure Speech fast transcription configuration
  actually sent (endpoint host, API version, locales, diarization, profanity mode),
  the post-processing dictionary (British spelling: the ``uwotm8`` package version),
  the application release and the generation time. LLM speaker naming and minutes
  are not covered here (OBS-6).

Investigators read it from the database, e.g.::

    SELECT j.id, j.created_datetime, j.provenance
    FROM transcriptionjob j WHERE j.transcription_id = '<transcription uuid>';

and SystemAdministrators see it on ``GET /api/admin/transcriptions/{id}``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import cache
from importlib import metadata
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from transcribe_api.runtime import version

logger = logging.getLogger(__name__)

DECISION_DOCUMENT = "decision_document"
TRANSCRIPT = "transcript"

SPEECH_FAST_TRANSCRIPTION = "azure-speech-fast-transcription"
BRITISH_SPELLING_PACKAGE = "uwotm8"


class SpeechConfiguration(BaseModel):
    """The Azure Speech request settings that produced a transcript."""

    model_config = ConfigDict(extra="forbid")

    service: str = SPEECH_FAST_TRANSCRIPTION
    # Host of the Speech resource endpoint (the deployment): no path, query or key.
    endpoint_host: str | None = None
    # Azure region, when the endpoint is a regional one (<region>.api.cognitive...).
    region: str | None = None
    api_version: str
    locales: list[str]
    diarization: bool
    profanity_filter_mode: str


class PostProcessingStep(BaseModel):
    """A deterministic step applied to the output after generation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    package: str
    version: str | None


class Provenance(BaseModel):
    """The shape of ``transcriptionjob.provenance``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    output: Literal["decision_document", "transcript"]
    # The release (APP_VERSION, set at image build: semver, or <semver>-<sha> for drafts).
    application_release: str = Field(default_factory=lambda: version.APP_VERSION)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    template: str | None = None
    template_version: str | None = None
    speech: SpeechConfiguration | None = None
    post_processing: list[PostProcessingStep] = Field(default_factory=list)

    def to_record(self) -> dict:
        """The JSON stored in the database."""
        return self.model_dump(mode="json")


def endpoint_reference(endpoint: str | None) -> tuple[str | None, str | None]:
    """(host, region) of a Speech endpoint URL; region only for regional endpoints."""
    if not endpoint:
        return None, None
    host = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}").hostname
    if not host:
        return None, None
    region = None
    for suffix in (".api.cognitive.microsoft.com", ".stt.speech.microsoft.com"):
        if host.endswith(suffix):
            region = host.removesuffix(suffix)
    return host, region


@cache
def package_version(package: str) -> str | None:
    """Installed version of a package, or None if unknown."""
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        logger.warning("Package version unknown for provenance (%s)", package)
        return None


def british_spelling_step() -> PostProcessingStep:
    """The American-to-British spelling conversion applied to every transcript."""
    return PostProcessingStep(
        name="british_spelling", package=BRITISH_SPELLING_PACKAGE, version=package_version(BRITISH_SPELLING_PACKAGE)
    )


def decision_document_provenance(*, template: str, template_version: str, generated_at: datetime) -> Provenance:
    return Provenance(
        output=DECISION_DOCUMENT, template=template, template_version=template_version, generated_at=generated_at
    )


def transcript_provenance(*, speech: SpeechConfiguration, generated_at: datetime) -> Provenance:
    return Provenance(
        output=TRANSCRIPT, speech=speech, post_processing=[british_spelling_step()], generated_at=generated_at
    )
