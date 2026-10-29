import asyncio
import ipaddress
import json
import logging
import math
import mimetypes
import re
import socket
import tempfile
import wave
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID, uuid4

import aiofiles
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from hmcts_otel import Journey, StageHandle, journey, record_event, stage
from hmcts_otel import conventions as c
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from transcribe_api.domain.audit_recording import (
    AuditRecord,
    AuditScope,
    DenialReason,
    action_for_violation,
    audited,
    denied,
    write_audit,
)
from transcribe_api.domain.auth.approles_recording import get_valid_roles
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.dependencies_recording import get_current_user
from transcribe_api.domain.interface_recording import (
    get_job_by_id,
    get_job_by_idempotency_key_for_user,
    list_jobs_paginated,
    record_correction_dataset_entry,
)
from transcribe_api.domain.models_recording import (
    CorrectionEntry,
    DialogueEntry,
    JobStatus,
    SpeechBatchJob,
    WordCorrection,
)
from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.runtime.security_events import record_ownership_violation
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import (
    EVENT_UPLOAD_VALIDATION_FAILED,
    JOURNEY_JOB,
    JOURNEY_UPLOAD,
    upload_id_from_blob,
)
from transcribe_api.runtime.version import APP_VERSION
from transcribe_api.stt import local_storage
from transcribe_api.stt.accuracy import DEFAULT_CONFIDENCE_THRESHOLD, compute_accuracy
from transcribe_api.stt.batch_client import BatchSubmissionError, delete_batch_job
from transcribe_api.stt.preprocessing import AudioDecodeError, normalize_to_wav
from transcribe_api.stt.speech_auth import SpeechAuthError
from transcribe_api.stt.submission import submit_and_queue_batch_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")

_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_METADATA_MAX_BYTES = 4096
# ~200MB comfortably covers a multi-hour hearing at typical speech bitrates
# while bounding worst-case memory use for a single upload.
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".mp4", ".m4a", ".ogg", ".flac"}
_VIDEO_EXTENSIONS = {".mp4"}
# DIAAT-280: the frontend sends one X-Hearing-Id for every file of a hearing, and the
# number of files, so multi-file uploads can be related in telemetry.
_MAX_HEARING_FILES = 50
_FILE_COUNT_RE = re.compile(r"^[0-9]{1,2}$")
_UNSAFE_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]+")
# A plain-text reference transcript for even a multi-hour hearing is at most
# a few hundred KB; 5MB is generous headroom without allowing an abusive upload.
_MAX_BASELINE_BYTES = 5 * 1024 * 1024
_ALLOWED_BASELINE_EXTENSIONS = {".txt"}


def _sanitize_filename(filename: str) -> str:
    """Collapse anything outside [A-Za-z0-9_.-] to "_".

    Keeps blob names uniformly safe for both storage backends: predictable
    for the local dev backend's path-traversal allowlist, and free of
    characters that would need URL-escaping in the returned audio_url.
    """
    name = PurePosixPath(filename).name or "audio"
    return _UNSAFE_FILENAME_CHARS_RE.sub("_", name)


# Rate limiter — keyed on the bearer token so limits are per-caller, not per-IP.
# Falls back to remote address for unauthenticated requests.
def _caller_key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else get_remote_address(request)


limiter = Limiter(key_func=_caller_key)


def _reject_private_url(url: str, field: str = "url") -> None:
    """Raise ValueError if url resolves to a private/internal address.

    Prevents SSRF: an attacker with valid credentials cannot point the
    service at the Azure metadata service (169.254.169.254), internal
    Postgres, or other VNet-internal hosts.
    Allowed in local environment to support docker-compose development.
    """
    if get_settings().ENVIRONMENT in ("local", "test"):
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field} must use http or https")
    host = parsed.hostname or ""
    try:
        # getaddrinfo returns all A/AAAA records; reject if any resolves to a restricted range.
        results = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"{field} hostname could not be resolved: {host}") from exc
    for _family, _type, _proto, _canonname, sockaddr in results:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"{field} resolves to a private/internal address: {ip}")


def _parse_range(range_header: str | None, total_size: int) -> tuple[int, int, bool]:
    """Parse a single-range HTTP Range header per RFC 9110.

    Returns (start, end, is_partial). Only a single "bytes=start-end" range
    is supported (including the suffix form "bytes=-N" for the last N
    bytes) — anything else we can't safely satisfy (malformed syntax,
    multi-range requests, an out-of-bounds range) is rejected with a 416
    rather than silently guessed at, since guessing wrong can otherwise
    produce a 206 with an empty or misaligned body that breaks <audio>
    seeking.
    """
    if total_size == 0:
        # Explicit branch rather than relying on total_size - 1 incidentally
        # producing a length-zero range — a zero-byte file has no bytes to
        # satisfy any Range request against.
        if range_header:
            raise HTTPException(
                status_code=416,
                detail="Range not satisfiable",
                headers={"Content-Range": "bytes */0"},
            )
        return 0, -1, False

    if not range_header:
        return 0, total_size - 1, False

    match = _RANGE_RE.match(range_header)
    if not match or not (match.group(1) or match.group(2)):
        raise HTTPException(
            status_code=416,
            detail="Invalid Range header",
            headers={"Content-Range": f"bytes */{total_size}"},
        )

    range_start, range_end = match.group(1), match.group(2)
    if range_start:
        start = int(range_start)
        end = int(range_end) if range_end else total_size - 1
    else:
        # Suffix range, e.g. "bytes=-500" means "the last 500 bytes".
        start = max(0, total_size - int(range_end))
        end = total_size - 1

    end = min(end, total_size - 1)
    if start < 0 or start > end or start >= total_size:
        raise HTTPException(
            status_code=416,
            detail="Range not satisfiable",
            headers={"Content-Range": f"bytes */{total_size}"},
        )
    return start, end, True


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SubmitJobRequest(BaseModel):
    audio_url: str
    # Blob name (as returned by POST /uploads) for the job's own audio, used
    # to serve it back for playback via GET /jobs/{id}/audio. Optional since
    # audio_url may point at storage this service doesn't own (e.g. a caller
    # submitting an already-public URL) — playback is simply unavailable then.
    blob_name: str | None = None
    locale: str = "en-GB"
    enable_diarization: bool = True
    idempotency_key: str | None = None
    # Total duration of the source audio, in seconds — supplied by the caller
    # (the frontend reads it client-side from the file before upload) since
    # Azure only reports it back once the batch job has already succeeded,
    # too late to show "Transcribing 2h 36m of audio" while still PROCESSING.
    audio_duration_seconds: float | None = None
    metadata: dict = Field(default_factory=dict)

    @field_validator("audio_duration_seconds")
    @classmethod
    def validate_audio_duration_seconds(cls, v: float | None) -> float | None:
        # Reject non-finite values (NaN/±Infinity) as well as negatives: Postgres
        # would store them but they can't be serialised back in a JSON response,
        # so they must not cross the API boundary in the first place.
        if v is not None and (not math.isfinite(v) or v < 0):
            raise ValueError("audio_duration_seconds must be a finite, non-negative number")
        return v

    @field_validator("audio_url")
    @classmethod
    def validate_audio_url(cls, v: str) -> str:
        _reject_private_url(v, "audio_url")
        return v

    @field_validator("locale")
    @classmethod
    def validate_locale(cls, v: str) -> str:
        if not _LOCALE_RE.match(v):
            raise ValueError("locale must be in the format 'en-GB'")
        return v

    @field_validator("metadata")
    @classmethod
    def validate_metadata_size(cls, v: dict) -> dict:
        if len(json.dumps(v, ensure_ascii=False).encode("utf-8")) > _METADATA_MAX_BYTES:
            raise ValueError(
                f"metadata must not exceed {_METADATA_MAX_BYTES} bytes when serialised"
            )
        return v

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 256:
            raise ValueError("idempotency_key must not exceed 256 characters")
        return v


class WordInfoResponse(BaseModel):
    text: str
    start_time: float
    end_time: float
    confidence: float


class WordCorrectionResponse(BaseModel):
    start_word_index: int
    end_word_index: int
    text: str


class NBestCandidateResponse(BaseModel):
    text: str
    confidence: float | None = None
    lexical: str | None = None


class PhraseAlternativesResponse(BaseModel):
    start_word_index: int | None = None
    end_word_index: int | None = None
    candidates: list[NBestCandidateResponse]


class CorrectionEntryResponse(BaseModel):
    timestamp: str
    kind: str
    previous_text: str
    new_text: str
    start_word_index: int | None = None
    end_word_index: int | None = None
    previous_phrase: str | None = None
    new_phrase: str | None = None


class DialogueEntryResponse(BaseModel):
    speaker: str
    text: str
    start_time: float
    end_time: float
    confidence: float | None = None
    corrected_text: str | None = None
    word_corrections: list[WordCorrectionResponse] | None = None
    correction_history: list[CorrectionEntryResponse] | None = None
    words: list[WordInfoResponse] | None = None
    alternatives: list[PhraseAlternativesResponse] | None = None
    accepted: bool = False


class NeedsReviewItemResponse(BaseModel):
    speaker: str
    start_time: float
    confidence: float


class AccuracyResponse(BaseModel):
    confidence_score: float
    words_transcribed: int
    low_confidence_count: int
    confidence_threshold: float
    has_corrections: bool
    word_error_rate: float | None = None
    corrected_percent: float | None = None
    has_baseline: bool = False
    baseline_word_error_rate: float | None = None


class JobResponse(BaseModel):
    job_id: UUID
    status: str
    created_at: str
    updated_at: str | None = None
    dialogue_entries: list[DialogueEntryResponse] | None = None
    accuracy: AccuracyResponse | None = None
    needs_review: list[NeedsReviewItemResponse] | None = None
    error_message: str | None = None
    metadata: dict = Field(default_factory=dict)
    # Run metadata (DIAAT-227): audio length, how long the transcription
    # itself took, and which model/engine produced it. audio_duration is
    # known from submission; the other two only once the job succeeds.
    audio_duration_seconds: float | None = None
    transcription_duration_seconds: float | None = None
    model_identifier: str | None = None
    # Human-readable model name resolved server-side (DIAAT-243) from the
    # Azure model.self URL. Null for historical jobs or when resolution
    # failed; the dashboard falls back to model_identifier. Contains only the
    # non-sensitive display name — never the Speech subscription key.
    model_display_name: str | None = None


class ListJobsResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    limit: int
    offset: int


class UploadResponse(BaseModel):
    audio_url: str
    blob_name: str


def _validate_not_blank(v: str) -> str:
    if not v.strip():
        raise ValueError("corrected_text must not be blank")
    return v


class CorrectSegmentRequest(BaseModel):
    corrected_text: str = Field(min_length=1, max_length=10_000)

    @field_validator("corrected_text")
    @classmethod
    def validate_corrected_text(cls, v: str) -> str:
        return _validate_not_blank(v)


class CorrectWordRangeRequest(BaseModel):
    start_word_index: int = Field(ge=0)
    end_word_index: int = Field(ge=0)
    corrected_text: str = Field(min_length=1, max_length=10_000)

    @field_validator("corrected_text")
    @classmethod
    def validate_corrected_text(cls, v: str) -> str:
        return _validate_not_blank(v)


class RollbackHistoryRequest(BaseModel):
    history_index: int = Field(ge=0)


def _entry_field(entry, field: str, default=None):
    return entry.get(field, default) if isinstance(entry, dict) else getattr(entry, field, default)


def _to_dialogue_entries(job: SpeechBatchJob) -> list[DialogueEntry] | None:
    if job.status != JobStatus.SUCCEEDED or not job.dialogue_entries:
        return None
    return [
        DialogueEntry(
            speaker=_entry_field(e, "speaker", ""),
            text=_entry_field(e, "text", ""),
            start_time=_entry_field(e, "start_time", 0.0),
            end_time=_entry_field(e, "end_time", 0.0),
            confidence=_entry_field(e, "confidence"),
            corrected_text=_entry_field(e, "corrected_text"),
            word_corrections=_entry_field(e, "word_corrections"),
            correction_history=_entry_field(e, "correction_history"),
            words=_entry_field(e, "words"),
            alternatives=_entry_field(e, "alternatives"),
            accepted=_entry_field(e, "accepted", False),
        )
        for e in job.dialogue_entries
    ]


def _to_response(job: SpeechBatchJob) -> JobResponse:
    entries = _to_dialogue_entries(job)
    accuracy = None
    needs_review = None
    if entries is not None:
        # LOW_CONFIDENCE_THRESHOLD lets ops tune the review-highlighting
        # cutoff per environment (e.g. via Key Vault) without a code change;
        # unset (the common case) falls back to the code default. Use an
        # explicit None check so an intentional 0.0 (flag nothing) is honoured
        # rather than treated as unset. Settings validates the 0-1 range.
        configured = get_settings().LOW_CONFIDENCE_THRESHOLD
        threshold = configured if configured is not None else DEFAULT_CONFIDENCE_THRESHOLD
        summary = compute_accuracy(
            entries,
            confidence_threshold=threshold,
            baseline_transcript=job.baseline_transcript,
        )
        accuracy = AccuracyResponse(
            confidence_score=summary.confidence_score,
            words_transcribed=summary.words_transcribed,
            low_confidence_count=summary.low_confidence_count,
            confidence_threshold=summary.confidence_threshold,
            has_corrections=summary.has_corrections,
            word_error_rate=summary.word_error_rate,
            corrected_percent=summary.corrected_percent,
            has_baseline=summary.has_baseline,
            baseline_word_error_rate=summary.baseline_word_error_rate,
        )
        needs_review = [
            NeedsReviewItemResponse(
                speaker=item.speaker, start_time=item.start_time, confidence=item.confidence
            )
            for item in summary.needs_review
        ]

    return JobResponse(
        job_id=job.id,
        status=job.status.value,
        created_at=job.created_datetime.isoformat(),
        updated_at=job.updated_datetime.isoformat() if job.updated_datetime else None,
        dialogue_entries=[
            DialogueEntryResponse(
                speaker=e.speaker,
                text=e.text,
                start_time=e.start_time,
                end_time=e.end_time,
                confidence=e.confidence,
                corrected_text=e.corrected_text,
                word_corrections=[
                    WordCorrectionResponse(
                        start_word_index=wc.start_word_index,
                        end_word_index=wc.end_word_index,
                        text=wc.text,
                    )
                    for wc in e.word_corrections
                ]
                if e.word_corrections
                else None,
                correction_history=[
                    CorrectionEntryResponse(
                        timestamp=h.timestamp,
                        kind=h.kind,
                        previous_text=h.previous_text,
                        new_text=h.new_text,
                        start_word_index=h.start_word_index,
                        end_word_index=h.end_word_index,
                        previous_phrase=h.previous_phrase,
                        new_phrase=h.new_phrase,
                    )
                    for h in e.correction_history
                ]
                if e.correction_history
                else None,
                words=[
                    WordInfoResponse(
                        text=w.text,
                        start_time=w.start_time,
                        end_time=w.end_time,
                        confidence=w.confidence,
                    )
                    for w in e.words
                ]
                if e.words
                else None,
                alternatives=[
                    PhraseAlternativesResponse(
                        start_word_index=pa.start_word_index,
                        end_word_index=pa.end_word_index,
                        candidates=[
                            NBestCandidateResponse(
                                text=c.text, confidence=c.confidence, lexical=c.lexical
                            )
                            for c in pa.candidates
                        ],
                    )
                    for pa in e.alternatives
                ]
                if e.alternatives
                else None,
                accepted=e.accepted,
            )
            for e in entries
        ]
        if entries is not None
        else None,
        accuracy=accuracy,
        needs_review=needs_review,
        error_message=job.error_message,
        metadata=job.metadata_,
        audio_duration_seconds=job.audio_duration_seconds,
        transcription_duration_seconds=job.transcription_duration_seconds,
        model_identifier=job.model_identifier,
        model_display_name=job.model_display_name,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/version")
async def version() -> dict:
    """Return the build version (see runtime/version.py), as /api/health does."""
    return {"version": APP_VERSION}


async def _read_upload_capped(
    file: UploadFile,
    max_bytes: int,
    *,
    error_detail: str = "Audio file exceeds the maximum upload size",
) -> bytes:
    """Read in chunks, aborting as soon as max_bytes is exceeded.

    Bounds worst-case memory use to roughly max_bytes rather than reading an
    arbitrarily oversized upload in a single call before checking its length.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=error_detail)
        chunks.append(chunk)
    return b"".join(chunks)


async def _save_upload_capped(file: UploadFile, max_bytes: int, target: Path) -> int:
    """Write the upload to ``target`` in chunks, aborting once max_bytes is exceeded.

    The audio upload goes to disk rather than memory: ffmpeg reads it from a
    file anyway, and up to 200 MB per request would otherwise sit in the API's
    memory alongside the decoded WAV. Returns the number of bytes written.
    """
    total = 0
    async with aiofiles.open(target, "wb") as out:
        while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(
                    status_code=413, detail="Audio file exceeds the maximum upload size"
                )
            await out.write(chunk)
    return total


def _hearing_id(raw: str | None) -> str | None:
    """A UUID from X-Hearing-Id, normalised; anything else is ignored (never exported)."""
    raw = (raw or "").strip()
    if not raw or len(raw) > 64:
        return None
    try:
        return str(UUID(raw))
    except ValueError:
        return None


def _hearing_file_count(raw: str | None) -> int | None:
    """X-Hearing-File-Count when it is an integer in 1..50, else None (not recorded)."""
    if raw and _FILE_COUNT_RE.fullmatch(raw) and 1 <= int(raw) <= _MAX_HEARING_FILES:
        return int(raw)
    return None


def _wav_duration_seconds(path: Path) -> float | None:
    """Duration of a PCM WAV file, or None if it cannot be read (telemetry only)."""
    try:
        with wave.open(str(path), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
    except Exception:  # a malformed header must never fail the upload
        return None
    return round(frames / rate, 3) if rate > 0 else None


def _upload_metadata(file: UploadFile, extension: str, file_count: int | None) -> dict[str, object]:
    """Safe technical metadata for the upload stage events. Never the file name."""
    attributes: dict[str, object] = {}
    if file_count is not None:
        attributes["upload.file_count"] = file_count
    if isinstance(file.size, int):
        attributes["file.size_bytes"] = file.size  # bytes received
    if extension in _ALLOWED_AUDIO_EXTENSIONS:
        attributes["audio.format"] = extension.lstrip(".")
        attributes["audio.format_category"] = "video" if extension in _VIDEO_EXTENSIONS else "audio"
    return attributes


def _reject(upload_stage: StageHandle, error_code: str) -> None:
    """Record a validation failure: its own event, then the stage's upload.failed."""
    upload_stage.fail(error_code)
    record_event(
        EVENT_UPLOAD_VALIDATION_FAILED,
        # The stage's validated code, so this event and upload.failed always agree.
        {
            c.STAGE: upload_stage.name,
            c.ERROR_CODE: upload_stage.error_code,
            **upload_stage.attributes,
        },
    )


@router.post("/uploads", status_code=201, response_model=UploadResponse)
@limiter.limit("50/hour")
async def upload_audio(
    request: Request,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    x_hearing_id: Annotated[str | None, Header()] = None,
    x_hearing_file_count: Annotated[str | None, Header()] = None,
) -> UploadResponse:
    # DIAAT-276: the upload is its own journey, identified by the UUID that also goes
    # into the blob name; the job created from it carries that ID as its parent.
    # DIAAT-280: the hearing (X-Hearing-Id) is the upload's parent, relating the files
    # of a multi-file hearing.
    upload_id = uuid4()
    extension = PurePosixPath(file.filename or "").suffix.lower()
    metadata = _upload_metadata(file, extension, _hearing_file_count(x_hearing_file_count))
    with (
        journey(JOURNEY_UPLOAD, str(upload_id), parent_id=_hearing_id(x_hearing_id)),
        stage("upload", attributes=metadata) as upload_stage,
    ):
        return await _upload_audio(file, extension, current_user, upload_id, upload_stage)


async def _upload_audio(
    file: UploadFile,
    extension: str,
    current_user: AuthenticatedUser,
    upload_id: UUID,
    upload_stage: StageHandle,
) -> UploadResponse:
    if extension not in _ALLOWED_AUDIO_EXTENSIONS:
        _reject(upload_stage, "upload.unsupported_format")
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type '{extension}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_AUDIO_EXTENSIONS))}",
        )

    safe_filename = _sanitize_filename(file.filename or "audio")
    wav_filename = f"{PurePosixPath(safe_filename).stem}.wav"
    blob_name = f"uploads/{current_user.id}/{upload_id}-{wav_filename}"

    # The upload and its WAV live in temp files and the WAV is streamed to
    # storage: the WAV is about 115 MB per hour of audio, and holding both in
    # memory could take a long recording past the pod's memory limit.
    with tempfile.TemporaryDirectory(prefix="upload-") as tmpdir:
        # The input keeps its extension so ffmpeg can sniff the source format.
        input_path = Path(tmpdir) / f"input{extension}"
        wav_path = Path(tmpdir) / "output.wav"
        try:
            size = await _save_upload_capped(file, _MAX_UPLOAD_BYTES, input_path)
        except HTTPException:  # _save_upload_capped raises only 413 (over the size limit)
            _reject(upload_stage, "upload.too_large")
            raise
        if "file.size_bytes" not in upload_stage.attributes:
            upload_stage.set_attribute("file.size_bytes", size)

        # Transcode every accepted upload to 16 kHz mono PCM WAV — the format
        # Azure Speech handles most reliably — before storing. A file that
        # cannot be decoded is rejected here rather than creating a job doomed
        # to fail with a cryptic "The recordings URI contains invalid data"
        # from Speech.
        try:
            await normalize_to_wav(input_path, wav_path)
        except AudioDecodeError as exc:
            _reject(upload_stage, "upload.undecodable")
            raise HTTPException(
                status_code=422,
                detail=(
                    "The audio file could not be decoded. "
                    "It may be corrupt or in an unsupported format."
                ),
            ) from exc
        except FileNotFoundError as exc:
            # ffmpeg is not installed: the server is misconfigured, the upload
            # is fine. Deployed pods check for it at startup (api/app.py), so
            # this should not happen; if it does, say so rather than 500.
            upload_stage.error("upload.transcoder_unavailable")  # server-side: counts as an error
            logger.error("Cannot transcode upload, ffmpeg is not available: %s", exc)
            raise HTTPException(
                status_code=503,
                detail=(
                    "Audio conversion is unavailable on the server. "
                    "Please try again later or contact support."
                ),
            ) from exc
        duration = await asyncio.to_thread(_wav_duration_seconds, wav_path)
        if duration is not None:
            upload_stage.set_attribute("audio.duration_seconds", duration)

        if get_settings().AUDIO_STORAGE_BACKEND == "local":
            local_storage.save_file(wav_path, blob_name)
            try:
                audio_url = local_storage.build_url(blob_name)
            except ValueError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            return UploadResponse(audio_url=audio_url, blob_name=blob_name)

        async with AsyncAzureBlobManager() as blob_manager:
            uploaded = await blob_manager.create_blob_from_file(wav_path, blob_name)
            if not uploaded:
                upload_stage.error("storage.write_failed")  # server-side: counts as an error
                raise HTTPException(status_code=502, detail="Failed to store audio file")
            audio_url = blob_manager.build_blob_url(blob_name)

    return UploadResponse(audio_url=audio_url, blob_name=blob_name)


@router.get("/local-audio/{blob_name:path}")
async def get_local_audio(blob_name: str) -> Response:
    """Serve locally-stored audio — only active in AUDIO_STORAGE_BACKEND=local.

    Deliberately unauthenticated: Azure Speech Batch fetches contentUrls
    directly and cannot send our bearer token. Safe because (a) this route
    404s outright unless a developer has explicitly opted into local storage,
    which never happens in a deployed environment, and (b) blob names embed
    an unguessable UUID.
    """
    if get_settings().AUDIO_STORAGE_BACKEND != "local":
        raise HTTPException(status_code=404)

    try:
        content = local_storage.read(blob_name)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Audio file not found") from None

    media_type = mimetypes.guess_type(blob_name)[0] or "application/octet-stream"
    return Response(content=content, media_type=media_type)


# DIAAT-285: the controlled security alert test is POST /api/security/alert-test
# (routes_dictation.py), one endpoint for both areas.


@router.post("/jobs", status_code=201, response_model=JobResponse)
@limiter.limit("100/hour")
async def submit_job(
    request: Request,
    body: Annotated[SubmitJobRequest, Body()],
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> JobResponse:
    # DIAAT-286: "did X submit Y". An idempotent replay is recorded too (detail.replayed).
    with audited("job.submit", current_user.id) as audit:
        job = await _submit_job(body, session, current_user, audit)
        audit.object_id = job.id
    return _to_response(job)


async def _submit_job(
    body: SubmitJobRequest,
    session: Session,
    current_user: AuthenticatedUser,
    audit: AuditScope,
) -> SpeechBatchJob:
    if body.idempotency_key:
        existing = get_job_by_idempotency_key_for_user(
            session, body.idempotency_key, current_user.id
        )
        if existing:
            audit.detail["replayed"] = True
            return existing

    # blob_name is later trusted by GET /jobs/{id}/audio to read straight from
    # storage — without this check a caller could point it at another user's
    # blob (upload_audio issues names under uploads/{current_user.id}/...) and
    # read their audio back through this job.
    if body.blob_name is not None and not body.blob_name.startswith(f"uploads/{current_user.id}/"):
        # DIAAT-285: a security event (types only; never the blob name), same 422.
        record_ownership_violation(
            "upload", "submit", "blocked", journey=_upload_journey(body.blob_name)
        )
        # DIAAT-286: the refusal is the audit record (naming the targeted upload by its
        # ID, never the blob name), in place of a "failed" submission.
        upload_id = _upload_uuid(body.blob_name)
        write_audit(denied("job.submit", current_user.id, upload_id, "not_owner", "upload"))
        audit.skip()
        raise HTTPException(status_code=422, detail="blob_name does not belong to this user")

    try:
        job = await submit_and_queue_batch_job(
            session=session,
            audio_url=body.audio_url,
            locale=body.locale,
            enable_diarization=body.enable_diarization,
            idempotency_key=body.idempotency_key,
            metadata=body.metadata,
            audio_duration_seconds=body.audio_duration_seconds,
            audio_blob_path=body.blob_name,
            user_id=current_user.id,
        )
    except IntegrityError:
        # Two concurrent requests with the same idempotency_key both passed the
        # check-then-act above; the second insert hit the unique constraint.
        # Roll back the failed transaction and return whatever the winner created.
        session.rollback()
        if body.idempotency_key:
            existing = get_job_by_idempotency_key_for_user(
                session, body.idempotency_key, current_user.id
            )
            if existing:
                audit.detail["replayed"] = True
                return existing
        raise HTTPException(
            status_code=409, detail="Concurrent submission conflict; retry"
        ) from None
    except (BatchSubmissionError, SpeechAuthError):
        # Speech rejected the job or could not be authenticated to. The job is
        # already FAILED with its idempotency key released, and the cause is
        # logged with its stack (stt/submission.py), so no traceback here.
        raise HTTPException(
            status_code=503,
            detail="The transcription service is unavailable. Please try again later.",
        ) from None

    return job


@router.get("/jobs", response_model=ListJobsResponse)
async def list_jobs(
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
    status: str | None = Query(default=None, description="Filter by job status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ListJobsResponse:
    parsed_status: JobStatus | None = None
    if status is not None:
        try:
            parsed_status = JobStatus(status)
        except ValueError:
            valid = ", ".join(s.value for s in JobStatus)
            raise HTTPException(
                status_code=400, detail=f"Invalid status '{status}'. Valid values: {valid}"
            ) from None

    # SystemAdministrators see all jobs (including pre-migration jobs with
    # user_id=NULL). Regular users see only their own JWT-created jobs;
    # pre-migration jobs are restricted to SystemAdministrator everywhere.
    is_admin = get_valid_roles()["SystemAdministrator"] in current_user.app_roles
    filter_user_id = None if is_admin else current_user.id
    jobs, total = list_jobs_paginated(session, filter_user_id, parsed_status, limit, offset)
    audit_records: list[AuditRecord] = []
    if not is_admin:
        # DIAAT-285: defence in depth. The query is already filtered by user, so a
        # foreign row here is a defect: drop it rather than return its transcript.
        owned = []
        for job in jobs:
            refusal = _ownership_refusal(job, current_user, "transcript", "read")
            if refusal is None:
                owned.append(job)
            else:
                audit_records.append(refusal)
        jobs = owned
    response = ListJobsResponse(
        jobs=[_to_response(j) for j in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )
    # DIAAT-286: only the refusals above are audited here (rare, and important). A list
    # is not recorded as a view of each transcript: the dashboard polls it every 5 s
    # while a job is in flight, which would bury the real events (see audit.py).
    write_audit(*audit_records)
    return response


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: UUID,
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> JobResponse:
    job = get_job_by_id(session, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _check_job_access(job, current_user, "transcript", "read")
    _assert_job_owner(job, current_user, "transcript", "read")
    # DIAAT-286: a view only when the transcript is returned (not while processing).
    with audited("transcript.view", current_user.id, job.id) as audit:
        response = _to_response(job)
        if response.dialogue_entries is None:
            audit.skip()
    return response


@router.post("/jobs/{job_id}/baseline", response_model=JobResponse)
async def upload_baseline_transcript(
    job_id: UUID,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> JobResponse:
    """Store a clerk-supplied reference transcript to compute a real WER against.

    Distinct from the correction-based WER (audio/accuracy.py), which only
    measures the segments a clerk has actually corrected in this app: this
    compares the *entire* auto-generated transcription against an
    independent reference transcript the clerk already has (e.g. a court
    reporter's transcript), so it stays meaningful even for segments nobody
    has reviewed yet, and is completely unaffected by corrections made here.
    Uploading again replaces any previously stored baseline.
    """
    job = get_job_by_id(session, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _check_job_access(job, current_user, "transcript", "write")
    if job.status != JobStatus.SUCCEEDED or not job.dialogue_entries:
        raise HTTPException(
            status_code=422, detail="Job has no transcript to compare a baseline against"
        )

    # Require a recognised extension (matching the audio upload endpoint):
    # a missing extension is rejected too, rather than silently accepted.
    extension = PurePosixPath(file.filename or "").suffix.lower()
    if extension not in _ALLOWED_BASELINE_EXTENSIONS:
        allowed = ", ".join(sorted(_ALLOWED_BASELINE_EXTENSIONS))
        detail = (
            f"File has no extension. Allowed: {allowed}"
            if not extension
            else f"Unsupported file type '{extension}'. Allowed: {allowed}"
        )
        raise HTTPException(status_code=422, detail=detail)

    content = await _read_upload_capped(
        file,
        _MAX_BASELINE_BYTES,
        error_detail="Baseline transcript exceeds the maximum upload size",
    )
    try:
        # utf-8-sig strips a leading BOM if present (common in Windows-exported
        # .txt files) so it isn't glued onto the first token and skewing the WER;
        # it decodes plain UTF-8 without a BOM identically.
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=422, detail="Baseline transcript must be UTF-8 encoded text"
        ) from None
    if not text.strip():
        raise HTTPException(status_code=422, detail="Baseline transcript file is empty")

    job.baseline_transcript = text
    job.updated_datetime = datetime.now(UTC)
    session.add(job)
    session.commit()
    session.refresh(job)
    return _to_response(job)


def _is_admin(current_user: AuthenticatedUser) -> bool:
    return get_valid_roles()["SystemAdministrator"] in current_user.app_roles


def _job_journey(job: SpeechBatchJob) -> Journey | None:
    try:
        return Journey(JOURNEY_JOB, str(job.id))
    except ValueError:  # not a valid identifier: record the event without it
        return None


def _upload_uuid(blob_name: str) -> UUID | None:
    """The upload ID in a blob name, if it has one (DIAAT-286 audit; never the name)."""
    upload_id = upload_id_from_blob(blob_name)
    try:
        return UUID(upload_id) if upload_id else None
    except ValueError:
        return None


def _upload_journey(blob_name: str) -> Journey | None:
    """The upload a rejected blob_name points at, if it has an upload ID (never the name)."""
    upload_id = upload_id_from_blob(blob_name)
    try:
        return Journey(JOURNEY_UPLOAD, upload_id) if upload_id else None
    except ValueError:
        return None


def _check_job_access(
    job: SpeechBatchJob,
    current_user: AuthenticatedUser,
    resource_type: str,
    action: str,
) -> None:
    """Raise 404 if current_user neither owns the job nor is a SystemAdministrator.

    404 is intentional: returning 403 would reveal that a job with this UUID
    exists, allowing enumeration. Callers already raised 404 for a missing job,
    so both cases look identical to the requester.

    Pre-migration jobs (user_id is None) were created by API-key callers before
    per-user auth existed. Both reads and writes on legacy jobs are restricted to
    SystemAdministrator — legacy jobs may contain sensitive hearing content, and
    any authenticated user who can obtain or guess a UUID should not be able to
    access that content.

    DIAAT-285: a refusal of an existing job is a security event
    (security.ownership_violation, outcome "blocked"). The job was already loaded
    by ID, so telling "not yours" from "not found" costs no extra query, and the
    response (404 "Job not found") is unchanged.
    """
    if _is_admin(current_user):
        return
    if job.user_id is None or job.user_id != current_user.id:
        reason: DenialReason = "legacy_job" if job.user_id is None else "not_owner"
        write_audit(_violation(job, current_user, resource_type, action, "blocked", reason))
        raise HTTPException(status_code=404, detail="Job not found")


def _violation(
    job: SpeechBatchJob,
    current_user: AuthenticatedUser,
    resource_type: str,
    action: str,
    outcome: str,
    reason: DenialReason,
) -> AuditRecord:
    """DIAAT-285's security event, and the DIAAT-286 audit record for the caller to write.

    The event carries no identifiers; the audit record (in the database) names the
    caller and the job, and shares the event's hmcts.request.id.
    """
    record_ownership_violation(resource_type, action, outcome, journey=_job_journey(job))
    return denied(action_for_violation(resource_type, action), current_user.id, job.id, reason)


def _ownership_refusal(
    job: SpeechBatchJob,
    current_user: AuthenticatedUser,
    resource_type: str,
    action: str,
) -> AuditRecord | None:
    """DIAAT-285: post-authorisation ownership assertion. None if the caller may have it.

    Independent of _check_job_access on purpose: it runs after authorisation has
    already passed, so it should never fail. If it does, an authorisation check
    is broken: record outcome "detected" (logged at ERROR) and return the audit
    record of the refusal (DIAAT-286) for the caller to write.
    """
    if _is_admin(current_user) or (job.user_id is not None and job.user_id == current_user.id):
        return None
    return _violation(
        job, current_user, resource_type, action, "detected", "ownership_assertion_failed"
    )


def _assert_job_owner(
    job: SpeechBatchJob,
    current_user: AuthenticatedUser,
    resource_type: str,
    action: str,
) -> None:
    """Block with the usual 404 if the post-authorisation ownership assertion fails."""
    refusal = _ownership_refusal(job, current_user, resource_type, action)
    if refusal is not None:
        write_audit(refusal)
        raise HTTPException(status_code=404, detail="Job not found")


def _load_entry_for_correction(
    session: Session, job_id: UUID, index: int, current_user: AuthenticatedUser
) -> tuple[SpeechBatchJob, DialogueEntry]:
    job = get_job_by_id(session, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _check_job_access(job, current_user, "transcript", "write")
    if job.status != JobStatus.SUCCEEDED or not job.dialogue_entries:
        raise HTTPException(status_code=422, detail="Job has no transcript to correct")
    if index < 0 or index >= len(job.dialogue_entries):
        raise HTTPException(status_code=404, detail="Segment not found")

    raw = job.dialogue_entries[index]
    entry = DialogueEntry(
        speaker=_entry_field(raw, "speaker", ""),
        text=_entry_field(raw, "text", ""),
        start_time=_entry_field(raw, "start_time", 0.0),
        end_time=_entry_field(raw, "end_time", 0.0),
        confidence=_entry_field(raw, "confidence"),
        corrected_text=_entry_field(raw, "corrected_text"),
        word_corrections=_entry_field(raw, "word_corrections"),
        correction_history=_entry_field(raw, "correction_history"),
        words=_entry_field(raw, "words"),
        alternatives=_entry_field(raw, "alternatives"),
        accepted=_entry_field(raw, "accepted", False),
    )
    return job, entry


def _save_corrected_entry(
    session: Session, job: SpeechBatchJob, index: int, entry: DialogueEntry
) -> None:
    # Reassign the whole list (rather than mutating in place) since the
    # dialogue_entries column isn't wrapped in sqlalchemy.ext.mutable —
    # in-place changes wouldn't be detected as dirty by the ORM.
    entries = list(job.dialogue_entries)
    entries[index] = entry.model_dump()
    job.dialogue_entries = entries
    job.updated_datetime = datetime.now(UTC)
    session.add(job)
    session.commit()
    session.refresh(job)


@router.patch("/jobs/{job_id}/segments/{index}", response_model=JobResponse)
async def correct_segment(
    job_id: UUID,
    index: int,
    body: CorrectSegmentRequest,
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> JobResponse:
    """Record a clerk's whole-segment correction.

    The original text is never overwritten — corrected_text/history is
    stored alongside it so a real word error rate can be computed against
    what Speech Batch actually produced (see audio/accuracy.py). A
    whole-segment override takes full precedence over any prior word-range
    corrections, so those are cleared.
    """
    job, entry = _load_entry_for_correction(session, job_id, index, current_user)

    previous_text = entry.effective_text()
    entry.corrected_text = body.corrected_text
    entry.word_corrections = None
    entry.correction_history = [
        *(entry.correction_history or []),
        CorrectionEntry(
            timestamp=datetime.now(UTC).isoformat(),
            kind="segment",
            previous_text=previous_text,
            new_text=body.corrected_text,
        ),
    ]

    # Dataset copy for future model training/eval (DIAAT-231) — gated behind
    # CORRECTIONS_DATASET_EXPORT_ENABLED, see record_correction_dataset_entry.
    # original_text is entry.text (the never-mutated ASR output), not
    # previous_text, so the training pair is always (ASR text, clerk text)
    # rather than (previous correction, latest correction).
    record_correction_dataset_entry(
        session,
        job=job,
        segment_index=index,
        correction_kind="segment",
        original_text=entry.text,
        corrected_text=body.corrected_text,
        confidence=entry.confidence,
        speaker=entry.speaker,
    )

    _save_corrected_entry(session, job, index, entry)
    return _to_response(job)


@router.patch("/jobs/{job_id}/segments/{index}/words", response_model=JobResponse)
async def correct_word_range(
    job_id: UUID,
    index: int,
    body: CorrectWordRangeRequest,
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> JobResponse:
    """Record a clerk's correction for just a run of words within a segment.

    Unlike a whole-segment correction, this keeps confidence highlighting
    and playback-sync intact for every word outside the corrected range.
    """
    job, entry = _load_entry_for_correction(session, job_id, index, current_user)

    if not entry.words:
        raise HTTPException(status_code=422, detail="Segment has no word-level data to correct")
    if body.start_word_index > body.end_word_index or body.end_word_index >= len(entry.words):
        raise HTTPException(status_code=422, detail="Invalid word range")
    if entry.corrected_text is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Segment has a whole-segment correction; roll it back before "
                "correcting an individual phrase"
            ),
        )

    previous_text = entry.effective_text()

    # What currently occupies exactly this range — an existing correction
    # over the identical range if the clerk is re-editing it, otherwise the
    # original words — captured before it's superseded below so the history
    # entry can show a concise "what changed" phrase instead of replaying
    # the whole (possibly very long) segment.
    existing_match = next(
        (
            wc
            for wc in (entry.word_corrections or [])
            if wc.start_word_index == body.start_word_index
            and wc.end_word_index == body.end_word_index
        ),
        None,
    )
    previous_phrase = (
        existing_match.text
        if existing_match
        else " ".join(w.text for w in entry.words[body.start_word_index : body.end_word_index + 1])
    )

    # Any existing correction overlapping the new range is superseded by it.
    non_overlapping = [
        wc
        for wc in (entry.word_corrections or [])
        if wc.end_word_index < body.start_word_index or wc.start_word_index > body.end_word_index
    ]
    non_overlapping.append(
        WordCorrection(
            start_word_index=body.start_word_index,
            end_word_index=body.end_word_index,
            text=body.corrected_text,
        )
    )
    entry.word_corrections = non_overlapping

    entry.correction_history = [
        *(entry.correction_history or []),
        CorrectionEntry(
            timestamp=datetime.now(UTC).isoformat(),
            kind="word_range",
            previous_text=previous_text,
            new_text=entry.effective_text(),
            start_word_index=body.start_word_index,
            end_word_index=body.end_word_index,
            previous_phrase=previous_phrase,
            new_phrase=body.corrected_text,
        ),
    ]

    # Dataset copy for future model training/eval (DIAAT-231) — gated behind
    # CORRECTIONS_DATASET_EXPORT_ENABLED, see record_correction_dataset_entry.
    # original_lexical_phrase is recomputed from entry.words (the never-
    # mutated original words) rather than reusing previous_phrase, which may
    # itself be a prior correction when the clerk is re-editing the same
    # range — the dataset always wants (ASR text, latest clerk text).
    original_lexical_phrase = " ".join(
        w.text for w in entry.words[body.start_word_index : body.end_word_index + 1]
    )
    range_confidences = [
        w.confidence
        for w in entry.words[body.start_word_index : body.end_word_index + 1]
        if w.confidence is not None
    ]
    range_confidence = (
        sum(range_confidences) / len(range_confidences) if range_confidences else entry.confidence
    )
    record_correction_dataset_entry(
        session,
        job=job,
        segment_index=index,
        correction_kind="word_range",
        original_text=original_lexical_phrase,
        corrected_text=body.corrected_text,
        confidence=range_confidence,
        speaker=entry.speaker,
        start_word_index=body.start_word_index,
        end_word_index=body.end_word_index,
    )

    _save_corrected_entry(session, job, index, entry)
    return _to_response(job)


@router.post("/jobs/{job_id}/segments/{index}/rollback", response_model=JobResponse)
async def rollback_segment(
    job_id: UUID,
    index: int,
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> JobResponse:
    """Revert a segment entirely back to its original Speech Batch output.

    Unlike rolling back to a specific history entry, this is a hard reset —
    every correction AND the history log itself are cleared, restoring
    original per-word confidence/timing highlighting exactly as Speech
    Batch produced it. It's a deliberate "start over" action, not one more
    logged change.
    """
    job, entry = _load_entry_for_correction(session, job_id, index, current_user)

    previous_text = entry.effective_text()
    if previous_text == entry.text and not entry.accepted:
        raise HTTPException(status_code=422, detail="Segment has no corrections to roll back")

    entry.corrected_text = None
    entry.word_corrections = None
    entry.correction_history = None
    entry.accepted = False

    _save_corrected_entry(session, job, index, entry)
    return _to_response(job)


@router.post("/jobs/{job_id}/segments/{index}/accept", response_model=JobResponse)
async def accept_segment(
    job_id: UUID,
    index: int,
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> JobResponse:
    """Mark a segment as reviewed/accepted without editing its text.

    Lets a clerk clear a low-confidence segment's "needs review" status by
    confirming the transcribed text is correct as spoken, instead of having
    to retype it verbatim just to satisfy has_corrections(). Recorded via
    the same correction_history audit trail as a real edit — kind=
    "accept_all" distinguishes it from an actual correction ("segment" /
    "word_range") or a rollback. previous_text/new_text are identical since
    nothing about the text changes.

    Unlike correct_segment/correct_word_range, this never sets
    corrected_text/word_corrections, so it never contributes to the
    word-error-rate calculation in audio/accuracy.py (there is nothing to
    compare against — no correction was made). needs_review filtering
    additionally excludes entries with accepted=True (see compute_accuracy).
    """
    job, entry = _load_entry_for_correction(session, job_id, index, current_user)

    if entry.accepted:
        raise HTTPException(status_code=422, detail="Segment has already been accepted")

    current_text = entry.effective_text()
    entry.accepted = True
    entry.correction_history = [
        *(entry.correction_history or []),
        CorrectionEntry(
            timestamp=datetime.now(UTC).isoformat(),
            kind="accept_all",
            previous_text=current_text,
            new_text=current_text,
        ),
    ]

    _save_corrected_entry(session, job, index, entry)
    return _to_response(job)


@router.post(
    "/jobs/{job_id}/segments/{index}/history/{history_index}/rollback",
    response_model=JobResponse,
)
async def rollback_to_history_entry(
    job_id: UUID,
    index: int,
    history_index: int,
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> JobResponse:
    """Revert a segment to how it looked immediately before one specific past edit.

    If the targeted entry was scoped to a specific word range (and nothing
    since has replaced that exact range or overridden the whole segment),
    this surgically undoes just that one correction — leaving every other
    correction and the per-word rendering for untouched words intact, and
    keeping this rollback's own history entry a concise phrase-level diff
    rather than a whole-segment wall of text. Anything less clean-cut
    (a later edit already touched this range, or a whole-segment freeform
    override is in effect) falls back to restoring a flat text snapshot,
    since reverting to an arbitrary point in time no longer has a clean
    correspondence to the original word positions in that case.
    """
    job, entry = _load_entry_for_correction(session, job_id, index, current_user)

    history = entry.correction_history or []
    if history_index < 0 or history_index >= len(history):
        raise HTTPException(status_code=404, detail="History entry not found")

    target = history[history_index]
    previous_text = entry.effective_text()

    # Anything currently overlapping the targeted range at all — not just an
    # exact match. An empty overlap means the range is presently untouched
    # (e.g. a previous rollback already reverted it to the original words,
    # and this is "undo that undo"), which is just as revertible as an
    # exact match; anything else (a *different*, only partially-overlapping
    # correction now covers part of this range) is genuinely ambiguous.
    overlapping = (
        [
            wc
            for wc in (entry.word_corrections or [])
            if not (
                wc.end_word_index < target.start_word_index
                or wc.start_word_index > target.end_word_index
            )
        ]
        if target.start_word_index is not None and target.end_word_index is not None
        else []
    )
    exact_match = next(
        (
            wc
            for wc in overlapping
            if wc.start_word_index == target.start_word_index
            and wc.end_word_index == target.end_word_index
        ),
        None,
    )
    can_revert_surgically = (
        target.start_word_index is not None
        and target.end_word_index is not None
        and target.previous_phrase is not None
        and entry.corrected_text is None
        and entry.words is not None
        and (not overlapping or exact_match is not None)
    )

    if can_revert_surgically:
        assert target.start_word_index is not None
        assert target.end_word_index is not None
        original_phrase = " ".join(
            w.text for w in entry.words[target.start_word_index : target.end_word_index + 1]
        )
        current_phrase = exact_match.text if exact_match else original_phrase
        remaining = [wc for wc in (entry.word_corrections or []) if wc is not exact_match]
        if target.previous_phrase != original_phrase:
            remaining.append(
                WordCorrection(
                    start_word_index=target.start_word_index,
                    end_word_index=target.end_word_index,
                    text=target.previous_phrase,
                )
            )
        entry.word_corrections = remaining or None
        entry.correction_history = [
            *history,
            CorrectionEntry(
                timestamp=datetime.now(UTC).isoformat(),
                kind="rollback",
                previous_text=previous_text,
                new_text=entry.effective_text(),
                start_word_index=target.start_word_index,
                end_word_index=target.end_word_index,
                previous_phrase=current_phrase,
                new_phrase=target.previous_phrase,
            ),
        ]
    else:
        restored_text = target.previous_text
        entry.corrected_text = None if restored_text == entry.text else restored_text
        entry.word_corrections = None
        entry.correction_history = [
            *history,
            CorrectionEntry(
                timestamp=datetime.now(UTC).isoformat(),
                kind="rollback",
                previous_text=previous_text,
                new_text=restored_text,
            ),
        ]

    _save_corrected_entry(session, job, index, entry)
    return _to_response(job)


@router.get("/jobs/{job_id}/audio")
async def get_job_audio(
    job_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Stream a job's source audio back for playback.

    The browser never talks to blob storage directly — it's deny-by-default
    outside this service's own managed identity — so this proxies the bytes
    through the same auth the rest of the API uses. Honours HTTP Range
    requests (returning 206 Partial Content) since browsers issue them when
    a user seeks to an unbuffered position — without this, seeking on an
    <audio> element that hasn't downloaded the whole file silently no-ops.
    """
    job = get_job_by_id(session, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _check_job_access(job, current_user, "audio", "read")
    _assert_job_owner(job, current_user, "audio", "read")
    # DIAAT-286: one record per request (a seek is its own Range request: partial).
    # A streamed (Azure) response is recorded when its body ends: success once sent,
    # failed on a storage error or a client disconnect.
    with audited("audio.export", current_user.id, job.id) as audit:
        return await _job_audio_response(job, request, audit)


async def _job_audio_response(job: SpeechBatchJob, request: Request, audit: AuditScope) -> Response:
    if not job.audio_blob_path:
        raise HTTPException(status_code=404, detail="Audio not available for this job")

    is_local = get_settings().AUDIO_STORAGE_BACKEND == "local"

    if is_local:
        try:
            total_size = local_storage.size(job.audio_blob_path)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="Audio file not found") from None
    else:
        async with AsyncAzureBlobManager() as blob_manager:
            total_size = await blob_manager.get_blob_size(job.audio_blob_path)
        if total_size is None:
            raise HTTPException(status_code=404, detail="Audio file not found")

    start, end, is_partial = _parse_range(request.headers.get("range"), total_size)
    audit.detail["partial"] = is_partial
    length = end - start + 1
    status_code = 206 if is_partial else 200

    media_type = mimetypes.guess_type(job.audio_blob_path)[0] or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if is_partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"

    if is_local:
        # Dev-only backend (see local_storage.py) — buffering the requested
        # range here is an accepted trade-off since it's never used with
        # production-sized recordings, unlike the Azure path below.
        try:
            content = local_storage.read_range(job.audio_blob_path, start, length)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="Audio file not found") from None
        return Response(
            content=content, media_type=media_type, status_code=status_code, headers=headers
        )

    # Streamed rather than buffered whole — a multi-hour recording served
    # via download_blob_range()'s readall() would otherwise spike memory
    # per concurrent playback request. Existence was already confirmed via
    # get_blob_size() above; a not-found error surfacing after this point
    # can no longer be turned into a clean 404 (headers are already sent),
    # same limitation any streaming file server has.
    #
    # The audited response closes the stream when the body ends, including after a
    # client disconnect left it suspended mid-body (Starlette stops iterating but
    # never closes it), rather than leaving its blob client and credential to the
    # garbage collector.
    return audit.streaming_response(
        _stream_blob_range(job.audio_blob_path, start, length),
        media_type=media_type,
        status_code=status_code,
        headers=headers,
    )


async def _stream_blob_range(blob_name: str, start: int, length: int) -> AsyncGenerator[bytes]:
    # The blob manager (and so its credential) is opened here rather than in
    # the handler, which returns before the body streams, so it lives exactly
    # as long as the stream. aclosing() closes stream_blob_range's own blob
    # client when this generator is closed early, since `async for` doesn't.
    async with (
        AsyncAzureBlobManager() as blob_manager,
        aclosing(blob_manager.stream_blob_range(blob_name, start, length)) as chunks,
    ):
        async for chunk in chunks:
            yield chunk


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(
    job_id: UUID,
    session: Session = Depends(get_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Permanently delete a job: its transcript (the DB row) and audio blob.

    Ownership is enforced by _check_job_access (404 for a non-owner who is
    not a SystemAdministrator). The audio blob is removed before the row so a
    genuine storage failure surfaces as a 502 with the record left intact,
    rather than orphaning audio behind a deleted row. A blob that's already
    gone is treated as success (idempotent). An in-flight Azure batch job is
    cleaned up best-effort so deleting a still-processing job doesn't leave it
    running.
    """
    job = get_job_by_id(session, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _check_job_access(job, current_user, "job", "delete")
    # DIAAT-286: success once the row is deleted; failed (http_502) if the audio could
    # not be deleted and the job was kept.
    with audited("job.delete", current_user.id, job.id):
        await _delete_job(session, job)
    return Response(status_code=204)


async def _delete_job(session: Session, job: SpeechBatchJob) -> None:
    if job.audio_blob_path:
        try:
            if get_settings().AUDIO_STORAGE_BACKEND == "local":
                local_storage.delete(job.audio_blob_path)
            else:
                async with AsyncAzureBlobManager() as blob_manager:
                    await blob_manager.delete_blob(job.audio_blob_path)
        except Exception as exc:
            logger.error("Failed to delete audio blob %s: %s", job.audio_blob_path, exc)
            raise HTTPException(
                status_code=502, detail="Failed to delete the audio file; job not deleted"
            ) from exc

    # Best-effort: an in-flight batch job would otherwise keep running on
    # Azure after its DB row is gone. Never blocks the delete.
    if job.batch_job_url:
        try:
            await delete_batch_job(job.batch_job_url)
        except Exception as exc:
            logger.warning("Could not delete batch job %s: %s", job.batch_job_url, exc)

    session.delete(job)
    session.commit()
