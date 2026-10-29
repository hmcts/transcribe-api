"""OBS-5: Azure Speech usage of batch jobs, recorded with hmcts-otel's AI usage metering.

What Azure Speech bills batch transcription on is the duration of the audio. Each job
whose transcript was saved records it once on ``hmcts.speech.audio.duration`` (mode
batch, feature batch_transcription): Azure's own duration from the results file when
it is there, else the duration given when the job was submitted (an estimate), else
nothing. Where the seconds came from is counted on transcription.speech.usage.jobs and
carried by job.finished. Usage only: no prices or cost anywhere.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from hmcts_otel import conventions as c
from hmcts_otel.testing import TelemetryCapture, capture_telemetry
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from transcribe_api.stt.batch_client import (
    BatchResultError,
    BatchResults,
    audio_duration_seconds,
    get_batch_results,
)
from transcribe_api.stt.job_telemetry import METRIC_SPEECH_USAGE_JOBS
from transcribe_api.stt.polling_service import BatchPollingService, _PendingJob
from transcribe_api.domain.models_recording import BatchJobStatus, JobStatus, SpeechBatchJob
from transcribe_api.runtime.telemetry import configure_telemetry

SPEECH_URL = (
    "https://uksouth.api.cognitive.microsoft.com/speechtotext/v3.2/transcriptions/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
)
BASE = "transcribe_api.stt.polling_service"
USAGE = {
    c.SPEECH_MODE: "batch",
    c.AI_FEATURE: "batch_transcription",
    c.OUTCOME: "success",
}


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):  # the unit suite has no Postgres
    return "JSON"


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


# Azure's duration in the results file ---------------------------------------------------


@pytest.mark.parametrize(
    ("result_data", "expected"),
    [
        ({"durationMilliseconds": 3_723_500}, 3_723.5),
        ({"durationInTicks": 37_235_000_000}, 3_723.5),
        ({"duration": "PT1H2M3.5S"}, 3_723.5),
        ({"duration": "PT45S"}, 45.0),
        ({"duration": "P1DT1S"}, 86_401.0),
        # The most precise field wins when Azure sends several.
        ({"durationMilliseconds": 1_000, "durationInTicks": 99, "duration": "PT9S"}, 1.0),
        ({"durationInTicks": 20_000_000, "duration": "PT9S"}, 2.0),
        # An invalid field falls through to the next one.
        ({"durationMilliseconds": -1, "durationInTicks": 30_000_000}, 3.0),
        ({"durationMilliseconds": "1000", "duration": "PT4S"}, 4.0),
        ({"durationInTicks": True, "duration": "PT5S"}, 5.0),
    ],
)
def test_azure_duration_is_read_from_the_top_of_the_results_file(result_data, expected):
    assert audio_duration_seconds(result_data) == pytest.approx(expected)


@pytest.mark.parametrize(
    "result_data",
    [
        {},
        {"recognizedPhrases": [{"durationInTicks": 20_000_000}]},  # a phrase, not the file
        {"durationMilliseconds": float("nan")},
        {"durationMilliseconds": float("inf")},
        {"durationInTicks": -5},
        {"durationInTicks": False},
        {"duration": "P"},
        {"duration": "PT"},
        {"duration": "1:02:03"},
        {"duration": "PT-5S"},
        {"duration": 12},
        [],
        None,
    ],
)
def test_no_valid_azure_duration_is_none(result_data):
    assert audio_duration_seconds(result_data) is None


def _client_returning(result_data: dict):
    files = {"values": [{"kind": "Transcription", "links": {"contentUrl": "https://results"}}]}

    def respond(url, **_kwargs):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.json.return_value = files if url.endswith("/files") else result_data
        return response

    client = AsyncMock()
    client.get = AsyncMock(side_effect=respond)
    return client


async def test_batch_results_carry_azures_duration_and_the_entries():
    result_data = {
        "durationMilliseconds": 5_400_000,
        "recognizedPhrases": [{"offsetInTicks": 0, "durationInTicks": 10_000_000, "nBest": [{"display": "Hi"}]}],
    }
    with patch("httpx.AsyncClient") as client_cls:
        client_cls.return_value.__aenter__ = AsyncMock(return_value=_client_returning(result_data))
        client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        results = await get_batch_results(SPEECH_URL)

    assert isinstance(results, BatchResults) and isinstance(results, list)
    assert [entry.text for entry in results] == ["Hi"]
    assert results.audio_duration_seconds == 5_400.0


async def test_batch_results_without_a_duration_still_parse():
    with patch("httpx.AsyncClient") as client_cls:
        client_cls.return_value.__aenter__ = AsyncMock(
            return_value=_client_returning({"recognizedPhrases": [], "duration": "garbage"})
        )
        client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        results = await get_batch_results(SPEECH_URL)

    assert results == [] and results.audio_duration_seconds is None


# Recorded once per succeeded job ----------------------------------------------------------


def _job(estimate: float | None = None) -> _PendingJob:
    return _PendingJob(
        id=uuid4(),
        batch_job_url=SPEECH_URL,
        batch_job_status=BatchJobStatus.RUNNING,
        callback_url=None,
        caller_id=None,
        metadata_={},
        created_datetime=datetime.now(UTC),
        audio_duration_seconds=estimate,
    )


def _service() -> BatchPollingService:
    service = BatchPollingService(webhook_secret_resolver=lambda _caller: "")
    service._save_results = MagicMock()
    service._record_error = MagicMock()
    service._update_status = MagicMock()
    service._record_cleanup_failure = MagicMock()
    return service


async def _poll(service, job, status="Succeeded", results=None) -> None:
    results = results if results is not None else AsyncMock(return_value=BatchResults([]))
    with (
        patch(f"{BASE}.get_batch_job_status", new=AsyncMock(return_value={"status": status})),
        patch(f"{BASE}.get_batch_results", new=results),
        patch(f"{BASE}.process_speakers", side_effect=lambda entries: list(entries)),
        patch(f"{BASE}._resolve_model_display_name", new=AsyncMock(return_value=None)),
        patch(f"{BASE}.delete_batch_job", new=AsyncMock()),
        patch(f"{BASE}.AsyncAzureBlobManager"),
    ):
        await service._process_job(job)


def _audio(tel: TelemetryCapture) -> list[dict]:
    return tel.metric_points(c.METRIC_SPEECH_AUDIO_DURATION)


def _sources(tel: TelemetryCapture) -> dict[str, float]:
    return {
        p["attributes"]["speech.audio_duration_source"]: p["value"] for p in tel.metric_points(METRIC_SPEECH_USAGE_JOBS)
    }


async def test_succeeded_job_records_azures_duration_once(tel):
    results = AsyncMock(return_value=BatchResults([], audio_duration_seconds=3_600.0))
    await _poll(_service(), _job(estimate=1.5), results=results)

    (point,) = _audio(tel)
    assert point["attributes"] == USAGE
    assert point["value"] == 3_600.0  # Azure's duration, not the 1.5 s estimate
    assert _sources(tel) == {"azure": 1}
    (finished,) = tel.events("job.finished")
    assert finished["speech.audio_duration_s"] == 3_600.0
    assert finished["speech.audio_duration_source"] == "azure"


@pytest.mark.parametrize(
    "results",
    [
        BatchResults([], audio_duration_seconds=None),  # Azure gave no duration
        [],  # a result without the field at all
    ],
    ids=["no_azure_duration", "plain_list"],
)
async def test_without_azures_duration_the_submitted_estimate_is_recorded(tel, results):
    await _poll(_service(), _job(estimate=90.0), results=AsyncMock(return_value=results))

    (point,) = _audio(tel)
    assert point["attributes"] == USAGE and point["value"] == 90.0
    assert _sources(tel) == {"estimate": 1}
    (finished,) = tel.events("job.finished")
    assert finished["speech.audio_duration_s"] == 90.0
    assert finished["speech.audio_duration_source"] == "estimate"


async def test_the_estimate_comes_from_the_jobs_row(tel, monkeypatch):
    """The poller reads audio_duration_seconds (given at submission) from the database."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            SpeechBatchJob(
                audio_url="https://x/y.wav",
                status=JobStatus.RUNNING,
                batch_job_status=BatchJobStatus.RUNNING,
                batch_job_url=SPEECH_URL,
                audio_duration_seconds=75.0,
            )
        )
        session.commit()
    monkeypatch.setattr(f"{BASE}.get_engine", lambda: engine)
    service = _service()

    (job,) = service._fetch_pending_jobs()
    assert job.audio_duration_seconds == 75.0
    # SQLite returns naive datetimes (PostgreSQL does not): give the poller an aware one.
    job = dataclasses.replace(job, created_datetime=datetime.now(UTC))
    await _poll(service, job, results=AsyncMock(return_value=[]))
    (point,) = _audio(tel)
    assert point["value"] == 75.0
    engine.dispose()


async def test_without_any_duration_nothing_is_recorded_but_the_job_is_counted(tel):
    await _poll(_service(), _job(estimate=None))

    assert _audio(tel) == []
    assert _sources(tel) == {"none": 1}
    (finished,) = tel.events("job.finished")
    assert finished[c.OUTCOME] == "success"
    assert "speech.audio_duration_s" not in finished
    assert "speech.audio_duration_source" not in finished


async def test_job_failed_in_azure_records_no_usage(tel):
    await _poll(_service(), _job(estimate=60.0), status="Failed")

    (finished,) = tel.events("job.finished")
    assert finished[c.OUTCOME] == "error"
    assert _audio(tel) == []
    assert _sources(tel) == {}
    assert "speech.audio_duration_s" not in finished


async def test_job_whose_results_cannot_be_retrieved_records_no_usage(tel):
    service = _service()
    await _poll(service, _job(estimate=60.0), results=AsyncMock(side_effect=BatchResultError("x")))

    (finished,) = tel.events("job.finished")
    assert finished[c.ERROR_CODE] == "speech.results_unavailable"
    assert _audio(tel) == [] and _sources(tel) == {}


async def test_usage_is_recorded_only_once_the_transcript_is_saved(tel):
    """A save that fails is retried on the next poll: the usage is recorded once, then."""
    service = _service()
    service._save_results = MagicMock(side_effect=[ConnectionError("db down"), None])
    job = _job()
    results = AsyncMock(return_value=BatchResults([], audio_duration_seconds=120.0))

    await _poll(service, job, results=results)  # Succeeded, but the save fails
    assert _audio(tel) == [] and _sources(tel) == {}
    assert tel.events("job.finished") == []

    await _poll(service, job, results=results)  # Succeeded and saved
    (point,) = _audio(tel)
    assert point["value"] == 120.0
    assert _sources(tel) == {"azure": 1}
    assert len(tel.events("job.finished")) == 1


async def test_one_poll_of_several_jobs_records_each_job_once(tel):
    service = _service()
    jobs = [_job(), _job(), _job()]
    results = AsyncMock(side_effect=[BatchResults([], audio_duration_seconds=s) for s in (60.0, 120.0, 30.0)])
    service._fetch_pending_jobs = MagicMock(return_value=jobs)
    with (
        patch(f"{BASE}.get_batch_job_status", new=AsyncMock(return_value={"status": "Succeeded"})),
        patch(f"{BASE}.get_batch_results", new=results),
        patch(f"{BASE}.process_speakers", side_effect=lambda entries: list(entries)),
        patch(f"{BASE}._resolve_model_display_name", new=AsyncMock(return_value=None)),
        patch(f"{BASE}.delete_batch_job", new=AsyncMock()),
    ):
        await service._poll_once()

    (point,) = _audio(tel)
    assert point["value"] == 210.0  # 60 + 120 + 30, each job once
    assert _sources(tel) == {"azure": 3}
    finished = tel.events("job.finished")
    assert sorted(e["speech.audio_duration_s"] for e in finished) == [30.0, 60.0, 120.0]


async def test_an_invalid_azure_duration_falls_back_to_the_estimate(tel):
    # BatchResults validates on parse; this guards the poller against any other value.
    results = AsyncMock(return_value=BatchResults([], audio_duration_seconds=float("nan")))
    await _poll(_service(), _job(estimate=45.0), results=results)

    (point,) = _audio(tel)
    assert point["value"] == 45.0
    assert _sources(tel) == {"estimate": 1}


async def test_usage_telemetry_carries_no_cost_and_no_content(tel):
    results = AsyncMock(return_value=BatchResults([], audio_duration_seconds=10.0))
    await _poll(_service(), _job(), results=results)

    names = {key for point in _audio(tel) + tel.metric_points(METRIC_SPEECH_USAGE_JOBS) for key in point["attributes"]}
    assert names == {c.SPEECH_MODE, c.AI_FEATURE, c.OUTCOME, "speech.audio_duration_source"}
    exported = tel.all_exported_text().lower()
    for word in ("cost", "price", "gbp", "usd"):
        assert word not in exported
