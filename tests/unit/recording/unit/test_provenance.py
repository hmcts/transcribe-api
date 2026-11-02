"""DIAAT-286 AC2: the configuration that produced a transcript, stored with the job.

The provenance names the Azure Speech resource, API version, locale, diarization,
model, Azure job, submission options, application release, generation time, the
spelling dictionary version and the speaker-processing steps; it is written in the
same transaction as the transcript and carries no content.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from transcribe_api.domain import provenance_recording as provenance_module
from transcribe_api.stt.polling_service import BatchPollingService, _PendingJob
from transcribe_api.stt.speakers_recording import process_speakers, speaker_step_report
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.domain.interface_recording import save_job_results
from transcribe_api.domain.models_recording import BatchJobStatus, DialogueEntry, SpeechBatchJob
from transcribe_api.domain.provenance_recording import TranscriptProvenance, build_provenance

SPEECH_JOB_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SPEECH_URL = f"https://test.cognitiveservices.azure.com/speechtotext/v3.2/transcriptions/{SPEECH_JOB_ID}"
MODEL_GUID = "e418c4a9-9937-4db7-b2c9-8afbff72d950"
STATUS = {
    "status": "Succeeded",
    "model": {"self": f"https://test.cognitiveservices.azure.com/speechtotext/models/base/{MODEL_GUID}"},
}
TRANSCRIPT_TEXT = "the appellant " + "fled " + "in 2019"
BASE = "transcribe_api.stt.polling_service"
STEPS = ["speaker_grouping", "speaker_normalisation", "speaker_labelling"]


@pytest.fixture(autouse=True)
def _release(monkeypatch):
    monkeypatch.setattr(provenance_module, "APP_VERSION", "abc1234")  # the release
    monkeypatch.setenv("AZURE_SPEECH_REGION", "uksouth")
    monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "https://test.cognitiveservices.azure.com")
    get_settings.cache_clear()


def _entries() -> list[DialogueEntry]:
    return [
        DialogueEntry(speaker="1", text=TRANSCRIPT_TEXT, start_time=0.0, end_time=1.0),
        DialogueEntry(speaker="2", text="No. " + TRANSCRIPT_TEXT, start_time=2.0, end_time=3.0),
    ]


def _build(**overrides) -> TranscriptProvenance | None:
    with speaker_step_report() as steps:
        process_speakers(_entries())
    arguments = {
        "locale": "en-GB",
        "diarization": True,
        "status_data": STATUS,
        "batch_job_url": SPEECH_URL,
        "model_display_name": "20240614 Base — en-GB",
        "speaker_steps": steps,
        **overrides,
    }
    return build_provenance(**arguments)


def test_provenance_records_the_configuration_that_produced_the_transcript():
    built = _build()

    assert built is not None
    assert built.model_dump(mode="json") == {
        "schema_version": 1,
        "app_release": "abc1234",
        "generated_at": None,  # stamped by save_job_results
        "speech": {
            "service": "azure-speech-batch-transcription",
            "endpoint_host": "test.cognitiveservices.azure.com",
            "region": "uksouth",
            "api_version": "v3.2",
            "locale": "en-GB",
            "diarization": True,
            "diarization_max_speakers": 35,
            "model_ref": MODEL_GUID,
            "model_display_name": "20240614 Base — en-GB",
            "azure_job_id": SPEECH_JOB_ID,
            "profanity_filter_mode": "None",
            "punctuation_mode": "DictatedAndAutomatic",
            "word_level_timestamps": True,
        },
        "post_processing": {
            "spelling_conversion": "uwotm8",
            "spelling_dictionary_version": version("uwotm8"),
            "speaker_steps_applied": STEPS,
            "speaker_step_degraded": None,
        },
    }


def test_without_diarization_there_is_no_speaker_limit():
    built = _build(diarization=False)
    assert built is not None
    assert built.speech.diarization is False
    assert built.speech.diarization_max_speakers is None


def test_a_failed_speaker_step_is_recorded_with_the_steps_before_it():
    with (
        patch(
            "transcribe_api.stt.speakers_recording.normalize_speaker_labels",
            side_effect=RuntimeError(TRANSCRIPT_TEXT),
        ),
        speaker_step_report() as steps,
    ):
        process_speakers(_entries())

    built = _build(speaker_steps=steps)
    assert built is not None
    assert built.post_processing.speaker_steps_applied == ["speaker_grouping"]
    assert built.post_processing.speaker_step_degraded == "speaker_normalisation"


def test_degradation_outside_a_report_is_not_collected():
    with patch(
        "transcribe_api.stt.speakers_recording.group_dialogue_entries_by_speaker",
        side_effect=RuntimeError("x"),
    ):
        process_speakers(_entries())  # no report active: nothing to collect, no error
    with speaker_step_report() as steps:
        pass
    assert steps.degraded is None
    assert steps.applied == STEPS


def test_provenance_never_carries_content():
    built = _build()
    assert built is not None
    assert TRANSCRIPT_TEXT not in json.dumps(built.model_dump(mode="json"))


def test_a_provenance_that_cannot_be_built_is_none_not_an_error(caplog):
    with patch.object(provenance_module, "speech_region", side_effect=RuntimeError("boom")):
        assert _build() is None
    assert "Could not build the transcript provenance (RuntimeError)" in caplog.text


# Saved with the transcript, in the same transaction ----------------------------------------


def _session_with(job: SpeechBatchJob) -> MagicMock:
    session = MagicMock()
    session.get.return_value = job
    return session


def test_save_job_results_stores_provenance_stamped_with_the_save_time():
    job = SpeechBatchJob(id=uuid4(), audio_url="https://x.example.com/a.wav")
    session = _session_with(job)
    built = _build()

    save_job_results(session, job.id, _entries(), BatchJobStatus.SUCCEEDED, provenance=built)

    session.commit.assert_called_once()  # one commit: the transcript and its provenance
    assert job.dialogue_entries and job.provenance is not None
    assert job.provenance["speech"]["model_ref"] == MODEL_GUID
    generated_at = datetime.fromisoformat(job.provenance["generated_at"])
    assert generated_at == job.updated_datetime
    assert datetime.now(UTC) - generated_at < timedelta(seconds=5)


def test_save_job_results_without_provenance_leaves_it_empty():
    job = SpeechBatchJob(id=uuid4(), audio_url="https://x.example.com/a.wav")

    save_job_results(_session_with(job), job.id, _entries(), BatchJobStatus.SUCCEEDED)

    assert job.provenance is None


# Built by the poller from the job and Azure's response --------------------------------------


def _pending(**fields) -> _PendingJob:
    return _PendingJob(
        id=uuid4(),
        batch_job_url=SPEECH_URL,
        batch_job_status=BatchJobStatus.RUNNING,
        callback_url=None,
        caller_id=None,
        metadata_={},
        locale="en-GB",
        enable_diarization=False,
        created_datetime=datetime.now(UTC) - timedelta(hours=1),
        **fields,
    )


def _service() -> BatchPollingService:
    service = BatchPollingService(webhook_secret_resolver=lambda _caller: "")
    service._save_results = MagicMock()
    return service


async def _poll(service: BatchPollingService, job: _PendingJob, **patches) -> None:
    with (
        patch(f"{BASE}.get_batch_job_status", new=AsyncMock(return_value=STATUS)),
        patch(f"{BASE}.get_batch_results", new=AsyncMock(return_value=_entries())),
        patch(f"{BASE}._resolve_model_display_name", new=AsyncMock(return_value="Base")),
        patch(f"{BASE}.delete_batch_job", new=AsyncMock()),
    ):
        await service._process_job(job)


async def test_the_poller_saves_the_provenance_with_the_results():
    service, job = _service(), _pending()

    await _poll(service, job)

    saved = service._save_results.call_args.args[6]
    assert isinstance(saved, TranscriptProvenance)
    assert saved.speech.azure_job_id == SPEECH_JOB_ID
    assert (saved.speech.locale, saved.speech.diarization) == ("en-GB", False)
    assert (saved.speech.model_ref, saved.speech.model_display_name) == (MODEL_GUID, "Base")
    assert saved.post_processing.speaker_steps_applied == STEPS


async def test_the_poller_records_a_degraded_speaker_step():
    service = _service()
    with patch("transcribe_api.stt.speakers_recording.add_speaker_labels", side_effect=RuntimeError("x")):
        await _poll(service, _pending())

    saved = service._save_results.call_args.args[6]
    assert saved.post_processing.speaker_step_degraded == "speaker_labelling"
    assert saved.post_processing.speaker_steps_applied == STEPS[:2]


async def test_the_transcript_is_saved_even_without_provenance():
    service = _service()
    with patch(f"{BASE}.build_provenance", return_value=None):
        await _poll(service, _pending())

    service._save_results.assert_called_once()
    assert service._save_results.call_args.args[6] is None
