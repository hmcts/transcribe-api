"""Azure Batch Transcription REST client."""

from __future__ import annotations

from uuid import UUID

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from uwotm8 import convert_american_to_british_spelling

from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.domain.models_recording import (
    DialogueEntry,
    NBestCandidate,
    PhraseAlternatives,
    WordInfo,
)

_TICKS_PER_SECOND: int = 10_000_000
_HTTP_TIMEOUT: float = 30.0
_HTTP_SERVER_ERROR_MIN: int = 500

_RETRY_POLICY = retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException)),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    stop=stop_after_attempt(2),
)


class BatchSubmissionError(Exception):
    """Azure rejected the batch transcription submission."""


class BatchResultError(Exception):
    """Batch transcription results could not be retrieved."""


def _auth_headers() -> dict[str, str]:
    return {"Ocp-Apim-Subscription-Key": get_settings().AZURE_SPEECH_KEY}


def _submit_url() -> str:
    # Path-versioned, no api-version query string: the "?api-version=..."
    # form 404s outright (route doesn't exist), confirmed against a real
    # Speech resource. Downstream calls (status/results/delete) use the
    # Location URL Azure itself returns from this call, so they don't need
    # a matching fix.
    endpoint = get_settings().AZURE_SPEECH_ENDPOINT.rstrip("/")
    return f"{endpoint}/speechtotext/v3.2/transcriptions"


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
            "speakers": {"minCount": 1, "maxCount": 5},
        }

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(_submit_url(), headers=_auth_headers(), json=payload)

    if response.status_code >= _HTTP_SERVER_ERROR_MIN:
        response.raise_for_status()

    if response.status_code != 201:
        raise BatchSubmissionError(
            f"Batch job submission failed: HTTP {response.status_code} — {response.text}"
        )

    job_url = response.headers.get("Location")
    if not job_url:
        raise BatchSubmissionError(
            "Azure did not return a Location header after batch job submission"
        )

    return job_url


@_RETRY_POLICY
async def get_batch_job_status(job_url: str) -> dict:
    """Return the full Azure batch job status JSON."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.get(job_url, headers=_auth_headers())
        response.raise_for_status()
    return response.json()


async def get_model_details(model_url: str) -> dict:
    """Dereference a Speech model resource (`model.self`) and return its JSON.

    The `model.self` URL Azure returns on a completed batch job is an
    authenticated REST endpoint ending in an opaque GUID. Fetching it yields
    the model's human-readable details (`displayName`, `locale`,
    `createdDateTime`, ...). The call reuses the backend's existing Speech
    subscription key via the same `Ocp-Apim-Subscription-Key` header as every
    other batch call — the key stays server-side and never leaves this
    process; only the parsed, non-sensitive display fields are surfaced to
    callers.

    Deliberately un-retried: resolution is best-effort and runs inline on the
    job-completion path (the caller catches failures), so a single fast
    attempt is preferred over the batch retry policy's multi-second backoff.
    """
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.get(model_url, headers=_auth_headers())
        response.raise_for_status()
    return response.json()


@_RETRY_POLICY
async def get_batch_results(
    job_url: str,
    transcription_job_id: UUID | None = None,
) -> list[DialogueEntry]:
    """Download and parse batch transcription results."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        files_response = await client.get(f"{job_url}/files", headers=_auth_headers())
        files_response.raise_for_status()
        files_data = files_response.json()

        transcription_files = [
            item for item in files_data.get("values", []) if item.get("kind") == "Transcription"
        ]
        if not transcription_files:
            raise BatchResultError(f"No transcription file in batch results for job: {job_url}")

        content_url = transcription_files[0].get("links", {}).get("contentUrl")
        if not content_url:
            raise BatchResultError(
                f"Transcription file missing contentUrl in batch results for job: {job_url}"
            )

        result_response = await client.get(content_url)
        result_response.raise_for_status()
        result_data = result_response.json()

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
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.delete(job_url, headers=_auth_headers())

    if response.status_code == 404:
        return

    response.raise_for_status()
