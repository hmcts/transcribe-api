"""DIAAT-287: the Phase 2 (recording) journey, end to end, as support would trace it.

AC2 ("a batch job has been traced end to end"), automated. Each scenario drives the
real FastAPI app (POST /uploads, POST /jobs, GET /jobs/{id}, GET /jobs/{id}/audio) and
the real BatchPollingService loop (run_polling_loop, one iteration per poll) through
the real Azure Speech client, database code and telemetry. Only the boundaries are
replaced: Azure Speech at HTTP (httpx.AsyncClient), blob storage (the blob manager),
the database (SQLite in memory) and the audit insert (captured, tests/conftest.py).

Scenarios: a success; an upload rejected by validation; Azure Speech reporting the
job Failed; throttling (a retry that recovers, then retries exhausted); a database
failure while saving the transcript; a stuck job; a multi-file hearing.

Multi-file stitching does not exist (DIAAT-283: one transcript file per job). A
multi-file hearing is several uploads sent with one X-Hearing-Id, each its own job
(DIAAT-280), so what is tested is that correlation: hearing -> uploads -> jobs.

Every scenario's exported telemetry is also scanned for prohibited content (AC3,
conftest.py): the case data below is fed in through file names, metadata, the
transcript, Azure's error message, SAS URLs, the user's email and the bearer token.
"""

from __future__ import annotations

import asyncio
import io
import wave
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from hmcts_otel import conventions as c
from hmcts_otel.testing import TelemetryCapture
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from tests.unit.recording.unit.validation.prohibited_content import (
    APPELLANT,
    AZURE_ERROR_MESSAGE,
    BEARER_TOKEN,
    CASE_REFERENCE,
    ORIGINAL_FILE_NAME,
    SAS_SIGNATURE,
    SPEAKER_TEXT,
    SPEECH_KEY,
    TRANSCRIPT_TEXT,
    USER_EMAIL,
)
from transcribe_api.stt import polling_service
from transcribe_api.stt.job_telemetry import METRIC_JOB_DURATION, METRIC_JOBS_FINISHED
from transcribe_api.stt.polling_service import BatchPollingService
from transcribe_api.stt.speech_telemetry import METRIC_SPEECH_THROTTLED
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.domain.interface_recording import save_job_results
from transcribe_api.domain.models_recording import JobStatus, SpeechBatchJob, User
from transcribe_api.runtime.telemetry import JOURNEY_JOB, JOURNEY_UPLOAD, upload_id_from_blob

ENDPOINT = "https://test.cognitiveservices.azure.com"
TRANSCRIPTIONS = f"{ENDPOINT}/speechtotext/v3.2/transcriptions"
MODEL_URL = f"{ENDPOINT}/speechtotext/v3.2/models/base/e418c4a9-9937-4db7-b2c9-8afbff72d950"
RESULTS_HOST = "https://spsvcprodukso.blob.core.windows.net"
STORAGE = "https://teststorage.blob.core.windows.net/test-container"
USER_ID = UUID("00000000-0000-4000-8000-0000000000c1")
HEARING_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):  # the unit suite has no Postgres
    return "JSON"


def _wav(seconds: float = 1.5) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * int(seconds * 16_000))
    return buffer.getvalue()


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


# Azure Speech, at the HTTP boundary -------------------------------------------------------


class FakeSpeech:
    """Azure Speech batch transcription: submit, status, results, model and delete.

    Each Azure job answers its status calls from a script (an item per call; the last
    one repeats): a status body (200), or (status code, body, headers) for an error.
    """

    def __init__(self) -> None:
        self.scripts: dict[str, list] = {}
        self.calls: list[tuple[str, str]] = []  # (method, what)
        self.submitted: list[dict] = []

    def job_ids(self) -> list[str]:
        return [s["id"] for s in self.submitted]

    def script(self, speech_job_id: str, *items) -> None:
        self.scripts[speech_job_id] = list(items)

    @staticmethod
    def _response(method: str, url: str, status: int, body=None, headers=None):
        request = httpx.Request(method, url)
        return httpx.Response(status, json=body, headers=headers, request=request)

    async def post(self, url: str, **kwargs):
        assert url == TRANSCRIPTIONS
        assert kwargs["headers"]["Ocp-Apim-Subscription-Key"] == SPEECH_KEY
        speech_job_id = str(uuid4())
        self.submitted.append({"id": speech_job_id, "payload": kwargs["json"]})
        self.scripts.setdefault(speech_job_id, [{"status": "NotStarted"}])
        self.calls.append(("POST", "submit"))
        headers = {"Location": f"{TRANSCRIPTIONS}/{speech_job_id}"}
        return self._response("POST", url, 201, {"status": "NotStarted"}, headers)

    async def get(self, url: str, **_kwargs):
        if url.startswith(RESULTS_HOST):
            self.calls.append(("GET", "results.content"))
            return self._response("GET", url, 200, _azure_results())
        if url == MODEL_URL:
            self.calls.append(("GET", "model"))
            model = {"displayName": "20240614 Base", "locale": "en-GB"}
            return self._response("GET", url, 200, model)
        speech_job_id = url.removeprefix(f"{TRANSCRIPTIONS}/").removesuffix("/files")
        if url.endswith("/files"):
            self.calls.append(("GET", "results.files"))
            content = (
                f"{RESULTS_HOST}/bestor-x/TranscriptionData/{speech_job_id}_0_0.json"
                f"?skoid=x&sv=2024-05-04&sig={SAS_SIGNATURE}"
            )
            files = {"values": [{"kind": "Transcription", "links": {"contentUrl": content}}]}
            return self._response("GET", url, 200, files)
        self.calls.append(("GET", "status"))
        script = self.scripts[speech_job_id]
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, tuple):
            return self._response("GET", url, *item)
        return self._response("GET", url, 200, {"self": url, **item})

    async def delete(self, url: str, **_kwargs):
        self.calls.append(("DELETE", "job"))
        return self._response("DELETE", url, 204)


def _azure_results() -> dict:
    def phrase(speaker: int, text: str, offset: int) -> dict:
        words = [
            {"word": w, "offsetInTicks": offset + i, "durationInTicks": 1, "confidence": 0.9}
            for i, w in enumerate(text.split())
        ]
        best = {"display": text, "lexical": text.lower(), "confidence": 0.91, "words": words}
        return {
            "speaker": speaker,
            "offsetInTicks": offset,
            "durationInTicks": 20_000_000,
            "nBest": [best],
        }

    return {"recognizedPhrases": [phrase(1, SPEAKER_TEXT, 0), phrase(2, TRANSCRIPT_TEXT, 30_000_000)]}


def _running(at: datetime) -> dict:
    return {"status": "Running", "lastActionDateTime": _iso(at)}


def _succeeded(created: datetime, at: datetime) -> dict:
    return {
        "status": "Succeeded",
        "createdDateTime": _iso(created),
        "lastActionDateTime": _iso(at),
        "model": {"self": MODEL_URL},
    }


def _failed(at: datetime) -> dict:
    error = {"code": "InvalidData", "message": AZURE_ERROR_MESSAGE}
    return {"status": "Failed", "lastActionDateTime": _iso(at), "properties": {"error": error}}


THROTTLED = (
    429,
    {"error": {"code": "TooManyRequests", "message": AZURE_ERROR_MESSAGE}},
    {"Retry-After": "17"},
)


# Blob storage ------------------------------------------------------------------------------


class FakeBlobs:
    """The blob store behind AsyncAzureBlobManager (routes and the poller)."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.metadata: dict[str, dict] = {}

    def __call__(self, *_args, **_kwargs) -> FakeBlobs:  # AsyncAzureBlobManager()
        return self

    async def __aenter__(self) -> FakeBlobs:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def create_blob_from_file(self, file_path, blob_name: str) -> bool:
        self.blobs[blob_name] = file_path.read_bytes()  # CNP stores the WAV from a file
        return True

    def build_blob_url(self, blob_name: str) -> str:
        return f"{STORAGE}/{blob_name}?sv=2024-05-04&se=2026-10-23&sp=r&sig={SAS_SIGNATURE}"

    async def get_blob_size(self, blob_name: str) -> int | None:
        return len(self.blobs[blob_name]) if blob_name in self.blobs else None

    async def stream_blob_range(self, blob_name: str, start: int, length: int):
        yield self.blobs[blob_name][start : start + length]

    async def close(self) -> None:
        pass

    async def set_blob_metadata(self, blob_name: str, metadata: dict) -> bool:
        self.metadata[blob_name] = metadata
        return True


# The service, wired to the fakes ----------------------------------------------------------


class Service:
    """The API and the poller of one deployment, sharing one database."""

    def __init__(self, engine, speech: FakeSpeech, blobs: FakeBlobs, client: TestClient) -> None:
        self.engine = engine
        self.speech = speech
        self.blobs = blobs
        self.client = client
        self.poller = BatchPollingService(webhook_secret_resolver=lambda _caller: "")

    # API ---------------------------------------------------------------------------------

    def upload(self, name: str = ORIGINAL_FILE_NAME, **headers) -> httpx.Response:
        return self.client.post("/api/v1/uploads", files={"file": (name, b"ID3 audio", "audio/mpeg")}, headers=headers)

    def submit(self, upload: dict) -> httpx.Response:
        body = {
            "audio_url": upload["audio_url"],
            "blob_name": upload["blob_name"],
            "audio_duration_seconds": 1.5,
            # The frontend derives these from the file name.
            "metadata": {"case_reference": CASE_REFERENCE, "title": APPELLANT},
        }
        return self.client.post("/api/v1/jobs", json=body)

    def upload_and_submit(self, **headers) -> tuple[str, str, httpx.Response]:
        """(upload ID, job ID, the POST /jobs response) for an accepted upload."""
        uploaded = self.upload(**headers)
        assert uploaded.status_code == 201, uploaded.text
        submitted = self.submit(uploaded.json())
        assert submitted.status_code == 201, submitted.text
        upload_id = upload_id_from_blob(uploaded.json()["blob_name"])
        assert upload_id is not None
        return upload_id, submitted.json()["job_id"], submitted

    # Poller ------------------------------------------------------------------------------

    def poll(self, times: int = 1) -> None:
        """One iteration of the real polling loop per call: poll, then the stuck check."""
        interval = get_settings().BATCH_POLL_INTERVAL_SECONDS

        async def sleep(seconds: float) -> None:
            if seconds == interval:  # the loop's own sleep ends the iteration
                self.poller._shutdown = True
            # anything else is the Speech client's retry backoff: no waiting

        with patch.object(asyncio, "sleep", sleep):
            for _ in range(times):
                self.poller._shutdown = False
                asyncio.run(self.poller.run_polling_loop())

    # Database ----------------------------------------------------------------------------

    def job(self, job_id: str) -> SpeechBatchJob:
        with Session(self.engine) as session:
            job = session.get(SpeechBatchJob, UUID(job_id))
            assert job is not None
            session.expunge(job)
            return job

    def backdate(self, job_id: str, created: datetime) -> None:
        """The job was created at ``created`` (time passes between polls)."""
        with Session(self.engine) as session:
            job = session.get(SpeechBatchJob, UUID(job_id))
            job.created_datetime = created
            session.add(job)
            session.commit()


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def service(tel, engine, monkeypatch) -> Iterator[Service]:
    from transcribe_api.api.app import create_app
    from transcribe_api.runtime.db_recording import get_session
    from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
    from transcribe_api.domain.auth.dependencies_recording import get_current_user

    # The deployment's settings, whatever earlier tests left in the environment.
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("AZURE_SPEECH_KEY", SPEECH_KEY)
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "azure")
    get_settings.cache_clear()

    speech = FakeSpeech()
    monkeypatch.setattr(httpx.AsyncClient, "post", lambda _self, url, **kw: speech.post(url, **kw))
    monkeypatch.setattr(httpx.AsyncClient, "get", lambda _self, url, **kw: speech.get(url, **kw))
    monkeypatch.setattr(httpx.AsyncClient, "delete", lambda _self, url, **kw: speech.delete(url, **kw))
    blobs = FakeBlobs()
    monkeypatch.setattr("transcribe_api.api.routes_recording.AsyncAzureBlobManager", blobs)
    monkeypatch.setattr(polling_service, "AsyncAzureBlobManager", blobs)

    async def normalise(_input_path, wav_path) -> None:
        wav_path.write_bytes(_wav())  # ffmpeg's output: 16 kHz mono PCM, written to a file

    monkeypatch.setattr("transcribe_api.api.routes_recording.normalize_to_wav", normalise)
    monkeypatch.setattr(polling_service, "get_engine", lambda: engine)

    user = User(
        id=USER_ID,
        email=USER_EMAIL,
        azure_user_id="clerk-azure-oid",
        role="Normal",
        created_datetime=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with Session(engine) as session:
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)

    def session_override():
        with Session(engine) as session:
            yield session

    # The suite turns FastAPI instrumentation off; the journey needs the request spans.
    monkeypatch.setenv("DISABLE_FASTAPI_INSTRUMENTATION", "false")
    from transcribe_api.runtime import settings_dictation

    settings_dictation.get_settings.cache_clear()
    app = create_app()
    current = AuthenticatedUser(db_user=user, app_roles=["Normal"])
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_session] = session_override
    client = TestClient(
        app,
        raise_server_exceptions=False,
        headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
    )
    yield Service(engine, speech, blobs, client)


# Queries, as support would run them -------------------------------------------------------


def _journey(tel: TelemetryCapture, journey_id: str) -> list[dict]:
    """customEvents | where customDimensions["hmcts.journey.id"] == id, in order."""
    return [e for e in tel.events() if e.get(c.JOURNEY_ID) == journey_id]


def _names(events: list[dict]) -> list[str]:
    return [str(e[c.CUSTOM_EVENT_NAME]) for e in events]


def _one(tel: TelemetryCapture, name: str, journey_id: str) -> dict:
    (event,) = [e for e in _journey(tel, journey_id) if e[c.CUSTOM_EVENT_NAME] == name]
    return event


def _durations(tel: TelemetryCapture) -> dict[str, float]:
    return {p["attributes"]["phase"]: p["sum"] for p in tel.metric_points(METRIC_JOB_DURATION)}


SUCCESS_LIFECYCLE = [
    "job.created",
    "speech_submission.started",
    "speech_submission.completed",
    "job.queued",
    "speech.job_running",
    "result_processing.started",
    "results_retrieval.started",
    "results_retrieval.completed",
    "transcript_postprocessing.started",
    "transcript_postprocessing.completed",
    "transcript_persistence.started",
    "transcript_persistence.completed",
    "transcript.available",
    "job.finished",
    "result_processing.completed",
]


# Success -----------------------------------------------------------------------------------


def test_successful_job_is_one_journey_from_upload_to_audio_export(service, tel, audit_capture):
    upload_id, job_id, submitted = service.upload_and_submit()
    (speech_job_id,) = service.speech.job_ids()

    # Azure starts the job 5 minutes after it was created and finishes 15 minutes later
    # (within the 30 minute stuck-waiting threshold: this job is never stuck).
    created = datetime.now(UTC) - timedelta(minutes=25)
    started, finished = created + timedelta(minutes=5), created + timedelta(minutes=20)
    service.backdate(job_id, created)
    service.speech.script(speech_job_id, {"status": "NotStarted"}, _running(started), _succeeded(started, finished))
    service.poll(3)  # NotStarted, Running, Succeeded

    viewed = service.client.get(f"/api/v1/jobs/{job_id}")
    exported = service.client.get(f"/api/v1/jobs/{job_id}/audio")

    # The user got the transcript and the audio back.
    assert viewed.status_code == 200 and viewed.json()["status"] == "succeeded"
    texts = [entry["text"] for entry in viewed.json()["dialogue_entries"]]
    assert TRANSCRIPT_TEXT in texts  # the content is in the record, never in telemetry
    assert exported.status_code == 200 and exported.content == _wav()

    # One journey for the upload, one for the job, with the upload as the job's parent.
    upload_events = _journey(tel, upload_id)
    assert _names(upload_events) == ["upload.started", "upload.completed"]
    assert {e[c.JOURNEY_TYPE] for e in upload_events} == {JOURNEY_UPLOAD}
    job_events = _journey(tel, job_id)
    assert _names(job_events) == SUCCESS_LIFECYCLE
    assert {e[c.JOURNEY_TYPE] for e in job_events} == {JOURNEY_JOB}
    assert {e[c.JOURNEY_PARENT_ID] for e in job_events} == {upload_id}
    # Every event once Azure accepted the job names Azure's job (DIAAT-282).
    accepted = job_events[_names(job_events).index("speech_submission.completed") :]
    assert {e[c.DEPENDENCY_REF] for e in accepted if c.DEPENDENCY_REF in e} == {speech_job_id}
    for name in ("job.queued", "speech.job_running", "transcript.available", "job.finished"):
        assert _one(tel, name, job_id)[c.DEPENDENCY_REF] == speech_job_id

    # Queue wait and processing are measured separately, within the end-to-end time.
    done = _one(tel, "job.finished", job_id)
    assert done[c.OUTCOME] == "success"
    assert done["job.queue_wait_ms"] == pytest.approx(300_000)
    assert done["job.processing_ms"] == pytest.approx(900_000)
    assert 1_500_000 <= done[c.DURATION_MS] < 1_560_000
    assert done["job.queue_wait_ms"] + done["job.processing_ms"] <= done[c.DURATION_MS]
    durations = _durations(tel)
    assert durations["queue_wait"] + durations["processing"] <= durations["end_to_end"]
    assert _one(tel, "speech.job_running", job_id)[c.DURATION_MS] == pytest.approx(300_000)

    # The request that created the job and the poller's processing are one trace.
    (request_span,) = [
        s
        for s in tel.finished_spans()
        if s.attributes.get(c.REQUEST_ID) == submitted.headers["x-request-id"]
        and s.attributes.get("http.route") == "/api/v1/jobs"
    ]
    processing = next(s for s in tel.finished_spans() if s.name == "result_processing")
    (link,) = processing.links
    assert link.context.trace_id == request_span.context.trace_id

    # DIAAT-286: audit rows, each with the request ID of the request that did it.
    rows = {row["action"]: row for row in audit_capture.rows}
    assert set(rows) == {"job.submit", "transcript.view", "audio.export"}
    for action, response in (
        ("job.submit", submitted),
        ("transcript.view", viewed),
        ("audio.export", exported),
    ):
        row = rows[action]
        assert (row["outcome"], row["object_id"], row["actor_id"]) == (
            "success",
            UUID(job_id),
            USER_ID,
        )
        assert row["request_id"] == response.headers["x-request-id"]
    assert _one(tel, "job.created", job_id)[c.REQUEST_ID] == rows["job.submit"]["request_id"]

    # DIAAT-286: provenance stored with the transcript.
    job = service.job(job_id)
    assert job.status == JobStatus.SUCCEEDED
    provenance = job.provenance
    assert provenance["speech"]["azure_job_id"] == speech_job_id
    assert provenance["speech"]["model_ref"] == MODEL_URL.rsplit("/", 1)[-1]
    assert provenance["speech"]["model_display_name"] == "20240614 Base — en-GB"
    assert provenance["speech"]["locale"] == "en-GB"
    assert provenance["generated_at"] is not None
    assert TRANSCRIPT_TEXT not in str(provenance)

    # Azure Speech saw submit, three status polls, results, the model and the delete.
    assert service.speech.calls.count(("GET", "status")) == 3
    assert ("DELETE", "job") in service.speech.calls
    (counted,) = tel.metric_points(METRIC_JOBS_FINISHED)
    assert counted["attributes"][c.OUTCOME] == "success" and counted["value"] == 1


# Failures ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "undecodable", "status", "code"),
    [
        (ORIGINAL_FILE_NAME.replace(".mp3", ".txt"), False, 422, "upload.unsupported_format"),
        (ORIGINAL_FILE_NAME, True, 422, "upload.undecodable"),
    ],
    ids=["unsupported_format", "undecodable"],
)
def test_rejected_upload_ends_its_journey_with_a_validation_code(
    service, tel, monkeypatch, name, undecodable, status, code
):
    if undecodable:
        from transcribe_api.stt.preprocessing import AudioDecodeError

        async def decode_fails(_input_path, _wav_path):
            raise AudioDecodeError("ffmpeg: invalid data in " + name)

        monkeypatch.setattr("transcribe_api.api.routes_recording.normalize_to_wav", decode_fails)

    response = service.upload(name)

    assert response.status_code == status
    (invalid,) = tel.events("upload.validation_failed")
    (failed,) = tel.events("upload.failed")
    assert invalid[c.ERROR_CODE] == failed[c.ERROR_CODE] == code
    assert failed[c.OUTCOME] == "failure"  # the user's file, not a service error
    assert _names(_journey(tel, invalid[c.JOURNEY_ID])) == [
        "upload.started",
        "upload.validation_failed",
        "upload.failed",
    ]
    assert service.blobs.blobs == {} and service.speech.submitted == []
    assert not [e for e in tel.events() if e.get(c.JOURNEY_TYPE) == JOURNEY_JOB]


def test_job_azure_speech_fails_is_attributed_to_the_dependency(service, tel):
    upload_id, job_id, _ = service.upload_and_submit()
    (speech_job_id,) = service.speech.job_ids()
    created = datetime.now(UTC) - timedelta(minutes=30)
    service.backdate(job_id, created)
    service.speech.script(speech_job_id, _running(created + timedelta(minutes=5)), _failed(datetime.now(UTC)))
    service.poll(2)

    names = _names(_journey(tel, job_id))
    assert names[-3:] == ["speech.job_running", "speech.job_failed", "job.finished"]
    failed = _one(tel, "speech.job_failed", job_id)
    finished = _one(tel, "job.finished", job_id)
    for event in (failed, finished):
        assert event[c.ERROR_CODE] == "speech.job_failed"
        assert event["hmcts.failure.source"] == "dependency"
        assert event["speech.error_code"] == "InvalidData"  # Azure's code, never its message
        assert event[c.DEPENDENCY_REF] == speech_job_id
        assert event[c.JOURNEY_PARENT_ID] == upload_id
    assert finished[c.OUTCOME] == "error"
    assert not tel.events("transcript.available")

    # The record keeps Azure's message for the user; the audio is marked failed.
    job = service.job(job_id)
    assert job.status == JobStatus.FAILED and job.error_message == AZURE_ERROR_MESSAGE
    (metadata,) = service.blobs.metadata.values()
    assert metadata["status"] == "permanently_failed"
    viewed = service.client.get(f"/api/v1/jobs/{job_id}")
    assert viewed.json()["status"] == "failed"
    (counted,) = tel.metric_points(METRIC_JOBS_FINISHED)
    assert counted["attributes"]["hmcts.failure.source"] == "dependency"


def test_throttling_is_retried_and_the_job_still_completes(service, tel):
    _, job_id, _ = service.upload_and_submit()
    (speech_job_id,) = service.speech.job_ids()
    created = datetime.now(UTC) - timedelta(minutes=20)
    service.backdate(job_id, created)
    started = created + timedelta(minutes=2)
    service.speech.script(
        speech_job_id,
        THROTTLED,  # poll 1: throttled, retried...
        _running(started),  # ...and the retry recovers
        THROTTLED,  # poll 2: throttled twice, retries exhausted
        THROTTLED,
        _succeeded(started, datetime.now(UTC)),  # poll 3
    )
    service.poll(3)

    retried = [e for e in _journey(tel, job_id) if e[c.CUSTOM_EVENT_NAME] == "job.retried"]
    assert [e["job.retry.attempt"] for e in retried] == [1, 1]
    for event in retried:
        assert event[c.ERROR_CODE] == "speech.throttled"
        assert event["job.retry.operation"] == "speech.status"
        assert event["speech.retry_after_s"] == 17.0
        assert event[c.DEPENDENCY_REF] == speech_job_id
    poll_failed = _one(tel, "job.poll_failed", job_id)  # once: poll 2 only
    assert poll_failed[c.ERROR_CODE] == "speech.throttled"
    assert poll_failed["hmcts.failure.source"] == "dependency"
    names = _names(_journey(tel, job_id))
    assert names.index("speech.job_running") < names.index("job.poll_failed")
    assert names.count("job.finished") == 1
    assert _one(tel, "job.finished", job_id)[c.OUTCOME] == "success"
    (throttled,) = tel.metric_points(METRIC_SPEECH_THROTTLED)
    assert throttled["value"] == 3
    assert service.job(job_id).status == JobStatus.SUCCEEDED


def test_database_failure_saving_the_transcript_recovers_on_the_next_poll(service, tel, monkeypatch):
    _, job_id, _ = service.upload_and_submit()
    (speech_job_id,) = service.speech.job_ids()
    created = datetime.now(UTC) - timedelta(minutes=20)
    service.backdate(job_id, created)
    started = created + timedelta(minutes=1)
    service.speech.script(speech_job_id, _running(started), _succeeded(started, datetime.now(UTC)))
    # A database error quotes the statement and its parameters: the transcript.
    outage = OperationalError("UPDATE transcription_job", {"text": TRANSCRIPT_TEXT}, Exception())
    calls = []

    def save(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise outage
        return save_job_results(*args, **kwargs)

    monkeypatch.setattr(polling_service, "save_job_results", save)
    service.poll(3)  # Running; Succeeded but the save fails; Succeeded and saved

    events = _journey(tel, job_id)
    names = _names(events)
    persistence_failed = _one(tel, "transcript_persistence.failed", job_id)
    assert persistence_failed[c.ERROR_CODE] == "storage.db_unavailable"
    storage = _one(tel, "job.storage_failed", job_id)
    assert storage["storage.operation"] == "transcript.save"
    assert storage[c.ERROR_CODE] == "storage.db_unavailable"
    poll_failed = _one(tel, "job.poll_failed", job_id)
    assert poll_failed[c.ERROR_CODE] == "storage.db_unavailable"
    assert poll_failed["hmcts.failure.source"] == "application"
    assert names.index("job.poll_failed") < names.index("transcript.available")
    assert names.count("transcript.available") == names.count("job.finished") == 1
    assert _one(tel, "job.finished", job_id)[c.OUTCOME] == "success"
    assert service.job(job_id).status == JobStatus.SUCCEEDED


def test_stuck_job_is_reported_once(service, tel):
    upload_id, job_id, _ = service.upload_and_submit()
    (speech_job_id,) = service.speech.job_ids()
    service.backdate(job_id, datetime.now(UTC) - timedelta(hours=2))  # threshold: 30 min
    service.poll(2)  # Azure still has not started it

    stuck = _one(tel, "job.stuck", job_id)
    assert stuck["job.state"] == "waiting"
    assert stuck["hmcts.failure.source"] == "queue"
    assert stuck[c.DEPENDENCY_REF] == speech_job_id
    assert stuck[c.JOURNEY_PARENT_ID] == upload_id
    assert 2 * 3_600_000 <= stuck[c.DURATION_MS] < 2 * 3_600_000 + 60_000
    assert not tel.events("job.finished")
    assert service.speech.calls.count(("GET", "status")) == 2


def test_multi_file_hearing_relates_its_jobs_through_the_hearing(service, tel):
    """Two files of one hearing: two uploads with the hearing as parent, two jobs.

    No stitching (DIAAT-283): each file is its own job and transcript. Support finds
    the hearing's jobs in two hops: uploads by parent (the hearing), jobs by parent
    (each upload).
    """
    headers = {"X-Hearing-Id": HEARING_ID, "X-Hearing-File-Count": "2"}
    first = service.upload_and_submit(**headers)
    second = service.upload_and_submit(**headers)
    created = datetime.now(UTC) - timedelta(minutes=30)
    for (_, job_id, _), speech_job_id in zip((first, second), service.speech.job_ids(), strict=True):
        service.backdate(job_id, created)
        started = created + timedelta(minutes=3)
        service.speech.script(speech_job_id, _running(started), _succeeded(started, datetime.now(UTC)))
    service.poll(2)

    uploads = {
        e[c.JOURNEY_ID]
        for e in tel.events()
        if e.get(c.JOURNEY_TYPE) == JOURNEY_UPLOAD and e.get(c.JOURNEY_PARENT_ID) == HEARING_ID
    }
    assert uploads == {first[0], second[0]}
    for event in tel.events("upload.completed"):
        assert event["upload.file_count"] == 2
    jobs = {
        e[c.JOURNEY_ID]
        for e in tel.events()
        if e.get(c.JOURNEY_TYPE) == JOURNEY_JOB and e.get(c.JOURNEY_PARENT_ID) in uploads
    }
    assert jobs == {first[1], second[1]}
    for _, job_id, _ in (first, second):
        assert _names(_journey(tel, job_id)) == SUCCESS_LIFECYCLE
        assert service.job(job_id).status == JobStatus.SUCCEEDED
