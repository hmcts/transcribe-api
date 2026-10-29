"""Azure Batch Transcription REST client."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from uwotm8 import convert_american_to_british_spelling

from transcribe_api.stt.job_telemetry import record_speech_retry, response_error_code
from transcribe_api.stt.speech_telemetry import SPEECH_API_VERSION, speech_call
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.stt.speech_auth import speech_auth_headers
from transcribe_api.domain.models_recording import (
    DialogueEntry,
    NBestCandidate,
    PhraseAlternatives,
    WordInfo,
)

# A fixed transcript for SPEECH_BACKEND=mock (see settings_recording.py). Two
# speakers, word-level timing and a low-confidence word so the review UI's
# highlighting has something to show — not a transcription of anything real.
_MOCK_DIALOGUE_ENTRIES: list[DialogueEntry] = [
    DialogueEntry(
        speaker="1",
        text="Good morning, this is a locally generated test transcript.",
        start_time=0.0,
        end_time=3.2,
        confidence=0.95,
        words=[
            WordInfo(text="Good", start_time=0.0, end_time=0.3, confidence=0.98),
            WordInfo(text="morning,", start_time=0.3, end_time=0.7, confidence=0.97),
            WordInfo(text="this", start_time=0.7, end_time=0.9, confidence=0.96),
            WordInfo(text="is", start_time=0.9, end_time=1.0, confidence=0.96),
            WordInfo(text="a", start_time=1.0, end_time=1.1, confidence=0.95),
            WordInfo(text="locally", start_time=1.1, end_time=1.6, confidence=0.6),
            WordInfo(text="generated", start_time=1.6, end_time=2.2, confidence=0.94),
            WordInfo(text="test", start_time=2.2, end_time=2.6, confidence=0.95),
            WordInfo(text="transcript.", start_time=2.6, end_time=3.2, confidence=0.93),
        ],
        alternatives=[
            PhraseAlternatives(
                start_word_index=0,
                end_word_index=8,
                candidates=[
                    NBestCandidate(
                        text="Good morning, this is a locally generated test transcript.",
                        confidence=0.95,
                        lexical="good morning this is a locally generated test transcript",
                    )
                ],
            )
        ],
    ),
    DialogueEntry(
        speaker="2",
        text="No real audio was sent to Azure Speech for this job.",
        start_time=3.5,
        end_time=6.4,
        confidence=0.92,
        words=[
            WordInfo(text="No", start_time=3.5, end_time=3.7, confidence=0.97),
            WordInfo(text="real", start_time=3.7, end_time=3.9, confidence=0.96),
            WordInfo(text="audio", start_time=3.9, end_time=4.3, confidence=0.95),
            WordInfo(text="was", start_time=4.3, end_time=4.5, confidence=0.95),
            WordInfo(text="sent", start_time=4.5, end_time=4.8, confidence=0.94),
            WordInfo(text="to", start_time=4.8, end_time=4.9, confidence=0.95),
            WordInfo(text="Azure", start_time=4.9, end_time=5.3, confidence=0.9),
            WordInfo(text="Speech", start_time=5.3, end_time=5.7, confidence=0.9),
            WordInfo(text="for", start_time=5.7, end_time=5.9, confidence=0.93),
            WordInfo(text="this", start_time=5.9, end_time=6.1, confidence=0.94),
            WordInfo(text="job.", start_time=6.1, end_time=6.4, confidence=0.93),
        ],
        alternatives=[
            PhraseAlternatives(
                start_word_index=0,
                end_word_index=10,
                candidates=[
                    NBestCandidate(
                        text="No real audio was sent to Azure Speech for this job.",
                        confidence=0.92,
                        lexical="no real audio was sent to azure speech for this job",
                    )
                ],
            )
        ],
    ),
]

# Prefix for job URLs submit_batch_job returns under SPEECH_BACKEND=mock.
# get_batch_job_status/get_batch_results/delete_batch_job check for it before
# short-circuiting: SPEECH_BACKEND=mock alone isn't enough, because flipping
# it on a database that already has real, pending Azure jobs (a non-mock
# job_url) would otherwise unconditionally mark them Succeeded with the
# canned transcript on the next poll, without ever consulting Azure.
_MOCK_JOB_URL_PREFIX = "mock://batch-jobs/"
_TICKS_PER_SECOND: int = 10_000_000
# Azure documents the ceiling on `diarization.speakers.maxCount` as "must be
# less than 36 and more or equal to the minCount property", so 35 is the
# highest value the service accepts. We ask for the full range rather than a
# lower guess because exceeding maxCount doesn't fail the job: the service
# quietly folds the surplus voices into the labels it is allowed to emit, and
# downstream that's indistinguishable from a diarisation bug. Court hearings
# can carry well over five participants (judge, several counsel, appellant,
# respondent, interpreter, witnesses, clerk).
_MAX_DIARIZATION_SPEAKERS: int = 35
_HTTP_TIMEOUT: float = 30.0
_HTTP_SERVER_ERROR_MIN: int = 500

_RETRY_POLICY = retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException)),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    stop=stop_after_attempt(2),
    # DIAAT-281: each retry is a job.retried event on the job's journey.
    before_sleep=record_speech_retry,
)


class BatchSubmissionError(Exception):
    """Azure rejected the batch transcription submission."""

    def __init__(self, message: str, response: httpx.Response | None = None) -> None:
        super().__init__(message)
        # DIAAT-282: Azure's status, error code and Retry-After, for telemetry only.
        self.response = response


class BatchResultError(Exception):
    """Batch transcription results could not be retrieved."""


class BatchResultsInvalidError(BatchResultError):
    """Azure returned batch results we could not parse (missing keys, invalid JSON).

    A dependency failure (``speech.results_invalid``), not a bug in our code.
    """

    # Read by job_telemetry.failure_code: no import of this module needed there.
    speech_failure_code = "speech.results_invalid"


# What a malformed Azure results payload raises while it is parsed.
_MALFORMED_RESULTS = (ValueError, TypeError, KeyError, IndexError, AttributeError)


def _invalid_results(job_url: str, exc: BaseException) -> BatchResultsInvalidError:
    # The exception type only: the message of a parsing error can quote the transcript.
    return BatchResultsInvalidError(
        f"Malformed batch results from Azure Speech for job: {job_url} ({type(exc).__name__})"
    )


async def _auth_headers() -> dict[str, str]:
    # Workload identity bearer token; the key header only for local dev.
    return await speech_auth_headers(get_settings().AZURE_SPEECH_KEY)


def _submit_url() -> str:
    # Path-versioned, no api-version query string: the "?api-version=..."
    # form 404s outright (route doesn't exist), confirmed against a real
    # Speech resource. Downstream calls (status/results/delete) use the
    # Location URL Azure itself returns from this call, so they don't need
    # a matching fix.
    endpoint = get_settings().AZURE_SPEECH_ENDPOINT.rstrip("/")
    return f"{endpoint}/speechtotext/{SPEECH_API_VERSION}/transcriptions"


@_RETRY_POLICY
async def submit_batch_job(
    audio_sas_url: str,
    display_name: str,
    locale: str = "en-GB",
    enable_diarization: bool = True,
) -> str:
    """Submit audio to Azure Batch Transcription.

    Returns the job URL from the Location response header.
    Raises BatchSubmissionError on non-201 response.
    """
    if get_settings().SPEECH_BACKEND == "mock":
        return f"{_MOCK_JOB_URL_PREFIX}{uuid4()}"

    payload: dict = {
        "contentUrls": [audio_sas_url],
        "locale": locale,
        "displayName": display_name,
        "properties": {
            "wordLevelTimestampsEnabled": True,
            "profanityFilterMode": "None",
            "punctuationMode": "DictatedAndAutomatic",
        },
    }

    if enable_diarization:
        payload["properties"]["diarizationEnabled"] = True
        payload["properties"]["diarization"] = {
            "enabled": True,
            "speakers": {"minCount": 1, "maxCount": _MAX_DIARIZATION_SPEAKERS},
        }

    headers = await _auth_headers()
    # DIAAT-282: every Azure Speech call is timed and counted (speech_telemetry).
    with speech_call("speech.submit") as call:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = call.observe(
                await client.post(_submit_url(), headers=headers, json=payload)
            )

    if response.status_code >= _HTTP_SERVER_ERROR_MIN:
        response.raise_for_status()

    if response.status_code != 201:
        # Never Azure's response text: it can echo the request, including the audio's
        # SAS URL and its signature. The status and Azure's error code (an identifier,
        # pattern-checked) only.
        azure_code = response_error_code(response)
        raise BatchSubmissionError(
            f"Batch job submission failed: HTTP {response.status_code}"
            + (f" ({azure_code})" if azure_code else ""),
            response=response,
        )

    job_url = response.headers.get("Location")
    if not job_url:
        raise BatchSubmissionError(
            "Azure did not return a Location header after batch job submission"
        )

    return job_url


class SpeechNotConfiguredError(Exception):
    """AZURE_SPEECH_ENDPOINT is not set."""


async def check_speech_available(timeout_s: float) -> None:
    """Health check (DIAAT-277): list at most one base model; raises on failure.

    ``GET .../speechtotext/<version>/models/base?top=1`` with the same endpoint and
    credential (the workload identity) as submissions: a read of the service catalogue,
    no audio and nothing billed. A failed token is a SpeechAuthError. The response body
    is never read. Not retried and not counted in the Speech call metrics, which
    describe job traffic.
    """
    settings = get_settings()
    if not settings.AZURE_SPEECH_ENDPOINT:
        raise SpeechNotConfiguredError
    headers = await _auth_headers()
    endpoint = settings.AZURE_SPEECH_ENDPOINT.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.get(
            f"{endpoint}/speechtotext/{SPEECH_API_VERSION}/models/base", headers=headers, params={"top": 1}
        )
    response.raise_for_status()


@_RETRY_POLICY
async def get_batch_job_status(job_url: str) -> dict:
    """Return the full Azure batch job status JSON."""
    if get_settings().SPEECH_BACKEND == "mock" and job_url.startswith(_MOCK_JOB_URL_PREFIX):
        now = datetime.now(UTC).isoformat()
        return {"status": "Succeeded", "createdDateTime": now, "lastActionDateTime": now}

    headers = await _auth_headers()
    with speech_call("speech.status") as call:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = call.observe(await client.get(job_url, headers=headers))
            response.raise_for_status()
    return response.json()


async def get_model_details(model_url: str) -> dict:
    """Dereference a Speech model resource (`model.self`) and return its JSON.

    The `model.self` URL Azure returns on a completed batch job is an
    authenticated REST endpoint ending in an opaque GUID. Fetching it yields
    the model's human-readable details (`displayName`, `locale`,
    `createdDateTime`, ...). The call carries the same Speech credentials as
    every other batch call (see stt/speech_auth.py), so callers must only pass
    URLs on our Speech host (polling_service._is_trusted_speech_url); only
    the parsed, non-sensitive display fields are surfaced to callers.

    Deliberately un-retried: resolution is best-effort and runs inline on the
    job-completion path (the caller catches failures), so a single fast
    attempt is preferred over the batch retry policy's multi-second backoff.
    """
    headers = await _auth_headers()
    with speech_call("speech.model") as call:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = call.observe(await client.get(model_url, headers=headers))
            response.raise_for_status()
    return response.json()


@_RETRY_POLICY
async def get_batch_results(
    job_url: str,
    transcription_job_id: UUID | None = None,
) -> list[DialogueEntry]:
    """Download and parse batch transcription results."""
    if get_settings().SPEECH_BACKEND == "mock" and job_url.startswith(_MOCK_JOB_URL_PREFIX):
        return list(_MOCK_DIALOGUE_ENTRIES)

    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        with speech_call("speech.results") as call:
            files_response = call.observe(
                await client.get(f"{job_url}/files", headers=headers)
            )
            files_response.raise_for_status()
        try:
            files_data = files_response.json()
            transcription_files = [
                item for item in files_data.get("values", []) if item.get("kind") == "Transcription"
            ]
            content_url = (
                transcription_files[0].get("links", {}).get("contentUrl")
                if transcription_files
                else None
            )
        except _MALFORMED_RESULTS as exc:
            raise _invalid_results(job_url, exc) from None
        if not transcription_files:
            raise BatchResultError(f"No transcription file in batch results for job: {job_url}")

        if not content_url:
            raise BatchResultError(
                f"Transcription file missing contentUrl in batch results for job: {job_url}"
            )

        # The transcript file, from Azure Speech's own storage (a SAS URL).
        with speech_call("speech.results") as call:
            result_response = call.observe(await client.get(content_url))
            result_response.raise_for_status()
        try:
            result_data = result_response.json()
        except _MALFORMED_RESULTS as exc:
            raise _invalid_results(job_url, exc) from None

    try:
        return _parse_results(result_data)
    except _MALFORMED_RESULTS as exc:
        raise _invalid_results(job_url, exc) from None


def _parse_results(result_data: dict) -> list[DialogueEntry]:
    """Dialogue entries from Azure's transcription result JSON."""
    dialogue_entries: list[DialogueEntry] = []

    for phrase in result_data.get("recognizedPhrases", []):
        n_best = phrase.get("nBest")
        if not n_best:
            continue

        best = n_best[0]
        start_time = phrase.get("offsetInTicks", 0) / _TICKS_PER_SECOND
        end_time = (
            phrase.get("offsetInTicks", 0) + phrase.get("durationInTicks", 0)
        ) / _TICKS_PER_SECOND
        speaker = str(phrase.get("speaker", 0))
        text = convert_american_to_british_spelling(best.get("display", ""))
        confidence = best.get("confidence")

        words = [
            WordInfo(
                text=w.get("word", ""),
                start_time=w.get("offsetInTicks", 0) / _TICKS_PER_SECOND,
                end_time=(w.get("offsetInTicks", 0) + w.get("durationInTicks", 0))
                / _TICKS_PER_SECOND,
                confidence=w.get("confidence", 0.0),
            )
            for w in best.get("words", [])
        ] or None

        # Azure only ever offers alternatives as whole alternate phrasings of
        # the entire phrase (nBest[]) — there's no per-word nBest/alternatives
        # array anywhere in the v3.2 response (see DIAAT-232 spike writeup).
        # Persist the full array, not just the top choice already captured
        # above as text/confidence/words, so it isn't silently discarded.
        candidates = [
            NBestCandidate(
                text=convert_american_to_british_spelling(candidate.get("display", "")),
                confidence=candidate.get("confidence"),
                lexical=candidate.get("lexical"),
            )
            for candidate in n_best
        ]
        alternatives = [
            PhraseAlternatives(
                start_word_index=0 if words else None,
                end_word_index=len(words) - 1 if words else None,
                candidates=candidates,
            )
        ]

        dialogue_entries.append(
            DialogueEntry(
                speaker=speaker,
                text=text,
                start_time=start_time,
                end_time=end_time,
                confidence=confidence,
                words=words,
                alternatives=alternatives,
            )
        )

    return dialogue_entries


@_RETRY_POLICY
async def delete_batch_job(job_url: str) -> None:
    """Delete a completed batch job from Azure. Non-fatal on HTTP 404."""
    if get_settings().SPEECH_BACKEND == "mock" and job_url.startswith(_MOCK_JOB_URL_PREFIX):
        return

    headers = await _auth_headers()
    with speech_call("speech.delete", ok_statuses=(404,)) as call:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = call.observe(await client.delete(job_url, headers=headers))

    if response.status_code == 404:
        return

    response.raise_for_status()
