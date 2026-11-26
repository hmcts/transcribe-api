"""DIAAT-408: recordings of 1 hour to 8 hours, end to end, through the real code.

The real routes (``POST /uploads``, ``POST /jobs``, ``DELETE /jobs/{id}``) over the local storage
backend, the real PreparationService (real ffmpeg: conversion, silence detection, cutting), the real
BatchPollingService and the mock Speech backend (every part is its own mock job with its own
transcript), and a SQLite file database. Files are real tone-and-silence MP3s (8 kHz mono, 8 kbps: 3.6 MB
per hour) made by ffmpeg in a temporary directory that is removed afterwards.

Every scenario runs twice: at a **scaled-down size** in the normal suite (Azure's 240 minutes is 30 seconds,
the 3 h 30 m part target 22 seconds, the 8 hour maximum 64 seconds: the same code and the same assertions),
and at the **real size** (1 h, 4 h, 5 h, 8 h) behind ``RUN_LONG_E2E=1`` (see conftest.py). The real run
writes its timings to ``LONG_E2E_REPORT`` (a file path) if that is set; ``LONG_E2E_DIR`` says where the
generated audio and the storage live (the default is the system temporary directory).
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.unit.recording.unit.async_upload_helpers import USER_ID, new_engine
from transcribe_api.api import routes_recording
from transcribe_api.api.app import create_app
from transcribe_api.domain import interface_recording as jobs
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.dependencies_recording import get_current_user
from transcribe_api.domain.models_recording import JobStatus, SpeechBatchJob, User
from transcribe_api.runtime import settings_dictation, settings_recording
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.stt import part_submission, polling_service, recording_limits, splitter
from transcribe_api.stt import preparation_service as prep
from transcribe_api.stt.polling_service import BatchPollingService
from transcribe_api.stt.preparation_service import PreparationService

# Every request the browser makes must come back well inside the gateway's 30 s: the brief says under 10.
REQUEST_BUDGET_SECONDS = 10.0
SILENCE_SECONDS = 4.0


@dataclass(frozen=True)
class Scale:
    name: str
    limit: float  # Azure's limit per file (240 minutes)
    target: float  # the part target (3 h 30 m)
    maximum: float  # the longest recording with both flags on (20 h; the small scale's 64 s is a stand-in)
    one_hour: float
    exact_limit: float  # a recording exactly at the limit: one part
    five_hours: float
    over_limit: float  # 4 h 01 m
    eight_hours: float
    over_maximum: float  # 20 h 02 m (the small scale's 68 s is just over its maximum)


REAL = Scale("real", 14_400, 12_600, 72_000, 3_600, 14_400, 18_000, 14_460, 28_800, 72_120)
# The same shape at 1/450: 30 s is 240 minutes, 64 s is 8 hours (three parts of about 21 s) and is also the small scale's maximum. The refusals
# are scaled by more than 1/450 (4 h 01 m would be 30.13 s): the header is refused on only beyond
# HEADER_TOLERANCE_SECONDS (2 s, for the encoder delay and padding that differ between ffmpeg versions),
# so a file meant to be refused from its header is more than that over, and one meant to be accepted
# (exactly at a limit) is within it.
SMALL = Scale("small", 30, 22, 64, 8, 30, 40, 34, 64, 68)

SCALES = [
    pytest.param(SMALL, id="small"),
    pytest.param(REAL, id="real", marks=pytest.mark.long_e2e),
]


def _pattern(total: float, parts: int) -> list[tuple[str, float]]:
    """Tone with a silence just before each ideal cut (total / parts, 2 x, ...), 4 s long, so every cut has a
    silence in its window. ``parts`` of 1 gives plain tone."""
    spacing = total / parts
    pattern: list[tuple[str, float]] = []
    at = 0.0
    for k in range(1, parts):
        start = k * spacing - min(200.0, spacing / 10)  # inside the window (a quarter of the spacing at most)
        pattern += [("t", start - at), ("s", SILENCE_SECONDS)]
        at = start + SILENCE_SECONDS
    pattern.append(("t", total - at))
    return pattern


def make_mp3(path: Path, pattern: list[tuple[str, float]]) -> None:
    """A real recording: tones and silences as a mono 8 kHz MP3 at 8 kbps, the smallest practical."""
    command = ["ffmpeg", "-nostdin", "-v", "error"]
    for kind, seconds in pattern:
        source = (
            f"sine=frequency=440:duration={seconds}:sample_rate=8000"
            if kind == "t"
            else f"anullsrc=d={seconds}:r=8000:cl=mono"
        )
        command += ["-f", "lavfi", "-i", source]
    inputs = "".join(f"[{i}]" for i in range(len(pattern)))
    command += [
        "-filter_complex", f"{inputs}concat=n={len(pattern)}:v=0:a=1", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "8k",
        "-y", str(path),
    ]  # fmt: skip
    subprocess.run(command, check=True)


# The environment -------------------------------------------------------------------------------------

_REPORT: list[dict] = []


@pytest.fixture(scope="module", autouse=True)
def write_report():
    yield
    path = os.environ.get("LONG_E2E_REPORT")
    if path and _REPORT:
        Path(path).write_text(json.dumps(_REPORT, indent=2))


@pytest.fixture
def workdir():
    base = tempfile.mkdtemp(prefix="long-e2e-", dir=os.environ.get("LONG_E2E_DIR"))
    yield Path(base)
    shutil.rmtree(base, ignore_errors=True)  # the generated audio and the storage: never left behind


@pytest.fixture(autouse=True)
def _no_rate_limit():
    from transcribe_api.api.routes_recording import limiter

    limiter.reset()
    yield
    limiter.reset()


class Harness:
    """The real API and services of one deployment, a way to run each step, and the timings."""

    def __init__(self, scale: Scale, workdir: Path, monkeypatch, mocker) -> None:
        self.scale, self.workdir, self.monkeypatch = scale, workdir, monkeypatch
        self.storage = workdir / "storage"
        self.timings: dict[str, float] = {}
        monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
        monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(self.storage))
        monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "http://api.test")
        monkeypatch.setenv("SPEECH_BACKEND", "mock")
        self.set_flags(True, True)
        settings = get_settings()
        if scale is SMALL:
            mocker.patch.object(settings, "MAX_AUDIO_DURATION_SECONDS", int(scale.maximum))
            mocker.patch.object(settings, "PART_TARGET_SECONDS", int(scale.target))
            mocker.patch.object(prep, "AZURE_BATCH_FILE_LIMIT_SECONDS", scale.limit)
            mocker.patch.object(recording_limits, "AZURE_BATCH_FILE_LIMIT_SECONDS", scale.limit)
        mocker.patch.object(settings, "PART_RETRY_BACKOFF_SECONDS", 0)
        self.engine = new_engine(workdir / "jobs.db")
        for module in (prep, part_submission, polling_service):
            mocker.patch.object(module, "get_engine", new=lambda: self.engine)
        # Timings of the steps that matter for the pod sizing: conversion, silence detection, cutting.
        self._spy(mocker, prep, "normalize_to_wav", "convert")
        self._spy(mocker, routes_recording, "normalize_to_wav", "convert in the request")
        self._spy(mocker, splitter, "detect_silences", "silencedetect")
        self._spy(mocker, splitter, "cut_segments", "split")
        app = create_app()
        user = AuthenticatedUser(
            db_user=User(id=USER_ID, azure_user_id="oid", email="judge@example.test", role="Judge"),
            ctx=Context("IAC", "Judge"),
        )
        app.dependency_overrides[get_current_user] = lambda: user

        def session():
            with Session(self.engine) as s:
                yield s

        app.dependency_overrides[get_session] = session
        self.client = TestClient(app, raise_server_exceptions=False)

    def _spy(self, mocker, module, name: str, label: str) -> None:
        real = getattr(module, name)

        async def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return await real(*args, **kwargs)
            finally:
                self.timings[label] = self.timings.get(label, 0.0) + time.perf_counter() - started

        mocker.patch.object(module, name, new=timed)

    def set_flags(self, async_preparation: bool, long_recordings: bool) -> None:
        self.monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", str(async_preparation).lower())
        self.monkeypatch.setenv("LONG_RECORDINGS_ENABLED", str(long_recordings).lower())
        settings_dictation.get_settings.cache_clear()
        settings_recording.get_settings.cache_clear()

    def audio(self, name: str, pattern: list[tuple[str, float]]) -> Path:
        path = self.workdir / f"{name}.mp3"
        started = time.perf_counter()
        make_mp3(path, pattern)
        self.timings[f"generate {name}"] = time.perf_counter() - started
        return path

    def upload(self, path: Path):
        started = time.perf_counter()
        with path.open("rb") as handle:
            response = self.client.post("/api/v1/uploads", files={"file": (path.name, handle, "audio/mpeg")})
        seconds = time.perf_counter() - started
        self.timings["upload request"] = seconds
        self.timings["upload bytes"] = float(path.stat().st_size)
        return response, seconds

    def submit(self, uploaded: dict):
        body = {"audio_url": uploaded["audio_url"], "blob_name": uploaded["blob_name"], "metadata": {}}
        started = time.perf_counter()
        response = self.client.post("/api/v1/jobs", json=body)
        seconds = time.perf_counter() - started
        self.timings["jobs request"] = seconds
        return response, seconds

    async def prepare(self) -> None:
        started = time.perf_counter()
        service = PreparationService()
        await service.poll_once()
        await service.wait_idle()
        self.timings["prepare (whole)"] = time.perf_counter() - started

    async def poll(self) -> None:
        poller = BatchPollingService(webhook_secret_resolver=lambda _c: "")
        await poller._poll_once()  # single-file jobs
        await poller._poll_parts_once()  # jobs in parts

    def job(self, job_id: str) -> SpeechBatchJob:
        with Session(self.engine) as session:
            row = session.get(SpeechBatchJob, UUID(job_id))
            session.expunge(row)
            return row

    def parts(self, job_id: str):
        with Session(self.engine) as session:
            return jobs.get_job_parts(session, UUID(job_id))

    def stored_files(self) -> list[Path]:
        return [p for p in self.storage.glob("*") if p.is_file()] if self.storage.exists() else []

    def record(self, scenario: str, **extra) -> None:
        _REPORT.append(
            {
                "scale": self.scale.name,
                "scenario": scenario,
                **{k: round(v, 2) for k, v in self.timings.items()},
                **extra,
            }
        )


@pytest.fixture(params=SCALES)
def h(request, workdir, monkeypatch, mocker):
    return Harness(request.param, workdir, monkeypatch, mocker)


def _accepted(h: Harness, path: Path) -> tuple[str, dict]:
    uploaded, upload_seconds = h.upload(path)
    assert uploaded.status_code == 201, uploaded.text
    assert upload_seconds < REQUEST_BUDGET_SECONDS  # the request converts nothing: it answers in seconds
    submitted, submit_seconds = h.submit(uploaded.json())
    assert submitted.status_code == 201 and submitted.json()["status"] == "pending", submitted.text
    assert submit_seconds < REQUEST_BUDGET_SECONDS
    return submitted.json()["job_id"], uploaded.json()


# Accepted recordings ---------------------------------------------------------------------------------


async def test_a_one_hour_recording_is_accepted_at_once_prepared_whole_and_transcribed(h):
    path = h.audio("one-hour", _pattern(h.scale.one_hour, 1))
    job_id, _ = _accepted(h, path)
    assert h.stored_files()  # only the original is there: nothing was converted in the request
    assert "convert in the request" not in h.timings and "convert" not in h.timings

    await h.prepare()
    await h.poll()

    row = h.job(job_id)
    assert row.status == JobStatus.SUCCEEDED and h.parts(job_id) == []  # no part rows: the single-file path
    assert row.audio_duration_seconds == pytest.approx(h.scale.one_hour, abs=0.2)
    assert [e["speaker"] for e in row.dialogue_entries] == ["Speaker 0", "Speaker 1"]  # as ever
    assert "split" not in h.timings and "silencedetect" not in h.timings
    h.record("1 h", file_bytes=path.stat().st_size)


async def test_a_recording_of_exactly_the_azure_limit_is_not_split(h):
    path = h.audio("exact-limit", _pattern(h.scale.exact_limit, 1))
    job_id, _ = _accepted(h, path)
    await h.prepare()
    await h.poll()
    assert h.job(job_id).status == JobStatus.SUCCEEDED and h.parts(job_id) == []
    h.record("4 h (the limit)", file_bytes=path.stat().st_size)


@pytest.mark.parametrize(("name", "parts"), [("five-hours", 2), ("eight-hours", 3)])
async def test_a_recording_over_the_limit_is_split_into_parts_each_within_the_limit_and_merged_in_order(h, name, parts):
    total = h.scale.five_hours if name == "five-hours" else h.scale.eight_hours
    path = h.audio(name, _pattern(total, parts))
    job_id, _ = _accepted(h, path)

    await h.prepare()
    rows = h.parts(job_id)
    assert h.job(job_id).status == JobStatus.SUBMITTED and len(rows) == parts
    assert max(p.duration_seconds for p in rows) <= h.scale.limit
    assert all(p.cut_kind == "silence" for p in rows[1:])  # every cut found its silence
    for before, after in itertools.pairwise(rows):
        assert after.offset_seconds == pytest.approx(before.offset_seconds + before.duration_seconds, abs=1e-6)
    assert rows[0].offset_seconds == 0.0
    assert sum(p.duration_seconds for p in rows) == pytest.approx(total, abs=0.2)  # the parts are the whole recording

    await h.poll()

    row = h.job(job_id)
    assert row.status == JobStatus.SUCCEEDED and all(p.blob_path is None for p in h.parts(job_id))
    entries = row.dialogue_entries
    assert [e["speaker"] for e in entries] == [f"Part {k} · Speaker {n}" for k in range(1, parts + 1) for n in (0, 1)]
    for k, part in enumerate(rows):  # each part's transcript starts at its own offset
        assert entries[2 * k]["start_time"] == pytest.approx(part.offset_seconds)
        assert entries[2 * k]["text"] == f"Part {k + 1} opens with speaker one."
    starts = [e["start_time"] for e in entries]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    # What was stored is the recording for playback (the full WAV) and nothing else: parts and original are gone.
    assert len(h.stored_files()) == 1
    h.record(f"{'5 h' if parts == 2 else '8 h'} in {parts} parts", file_bytes=path.stat().st_size, parts=parts)


# Refused recordings ----------------------------------------------------------------------------------


def test_with_both_flags_off_a_recording_over_the_limit_is_refused_from_the_header_before_it_is_converted(h):
    h.set_flags(False, False)
    path = h.audio("over-limit", _pattern(h.scale.over_limit, 1))

    response, seconds = h.upload(path)

    assert response.status_code == 422
    assert (
        response.json()["detail"].startswith("The recording is ")
        and "Recordings can be at most" in response.json()["detail"]
    )
    # DIAAT-408: the header check runs whatever the flag, so nothing is converted or stored.
    assert seconds < REQUEST_BUDGET_SECONDS and "convert in the request" not in h.timings and h.stored_files() == []
    h.record("4 h 01 m, flags off (refused)", file_bytes=path.stat().st_size, refused_after=round(seconds, 2))


def test_with_background_preparation_on_but_long_recordings_off_it_is_refused_from_the_header_in_seconds(h):
    h.set_flags(True, False)
    path = h.audio("over-limit", _pattern(h.scale.over_limit, 1))

    response, seconds = h.upload(path)

    assert response.status_code == 422 and "Recordings can be at most" in response.json()["detail"]
    assert seconds < REQUEST_BUDGET_SECONDS and "convert in the request" not in h.timings and h.stored_files() == []
    h.record("4 h 01 m, async on, long off (refused)", file_bytes=path.stat().st_size, refused_after=round(seconds, 2))


def test_with_both_flags_on_a_recording_over_the_maximum_is_refused_from_the_header_in_seconds(h):
    path = h.audio("over-maximum", _pattern(h.scale.over_maximum, 1))

    response, seconds = h.upload(path)

    assert response.status_code == 422 and "Recordings can be at most" in response.json()["detail"]
    assert seconds < REQUEST_BUDGET_SECONDS and "convert in the request" not in h.timings and h.stored_files() == []
    h.record("over the maximum (20 h 02 m at the real size, refused)", file_bytes=path.stat().st_size, refused_after=round(seconds, 2))


SENTENCE = "The audio file could not be decoded. It may be corrupt or in an unsupported format."


async def test_a_file_that_is_not_audio_is_refused_in_the_request_with_the_flags_off(h):
    h.set_flags(False, False)
    bad = h.workdir / "corrupt.mp3"
    bad.write_bytes(b"this is not audio " * 4096)
    response, _ = h.upload(bad)
    assert response.status_code == 422 and response.json()["detail"] == SENTENCE and h.stored_files() == []


async def test_a_file_that_is_not_audio_fails_cleanly_with_background_preparation_on(h):
    """Observed with the ffmpeg of this environment: ffprobe exits 0 and reports no duration for text named .mp3
    (its MP3 detection is lenient), so the request cannot refuse it and the worker does: a FAILED job with
    the same sentence, its original deleted, nothing submitted. (A file ffprobe refuses is a 422 in the request.)"""
    bad = h.workdir / "corrupt.mp3"
    bad.write_bytes(b"this is not audio " * 4096)
    uploaded, seconds = h.upload(bad)
    assert seconds < REQUEST_BUDGET_SECONDS
    if uploaded.status_code == 422:
        assert uploaded.json()["detail"] == SENTENCE and h.stored_files() == []
        return
    assert uploaded.status_code == 201
    submitted, _ = h.submit(uploaded.json())
    await h.prepare()
    row = h.job(submitted.json()["job_id"])
    assert row.status == JobStatus.FAILED and row.error_message == SENTENCE and row.batch_job_id is None
    assert h.parts(str(row.id)) == [] and h.stored_files() == []  # the original is deleted; no WAV was ever stored
    h.record("corrupt file, async on")


# A job deleted while its parts are in flight ------------------------------------------------------------


async def test_a_job_deleted_while_its_parts_are_being_submitted_leaves_nothing_behind(h, monkeypatch):
    path = h.audio("eight-hours", _pattern(h.scale.eight_hours, 3))
    job_id, _ = _accepted(h, path)
    real_submit = part_submission.submit_batch_job
    deleted: list[str] = []
    made: list[str] = []

    async def submit(*args, **kwargs):
        if kwargs.get("display_name", "").endswith("part-2"):
            assert h.client.delete(f"/api/v1/jobs/{job_id}").status_code == 204  # the clerk deletes it meanwhile
        url = await real_submit(*args, **kwargs)
        made.append(url)
        return url

    async def delete(url):
        deleted.append(url)

    monkeypatch.setattr(part_submission, "submit_batch_job", submit)
    monkeypatch.setattr(part_submission, "delete_batch_job", delete)
    monkeypatch.setattr(prep, "delete_batch_job", delete)

    await h.prepare()

    with Session(h.engine) as session:
        assert session.get(SpeechBatchJob, UUID(job_id)) is None
    assert h.parts(job_id) == []
    assert h.stored_files() == []  # no original, no full WAV, no part blob
    # Every Azure job that was made is deleted: those before the delete by the delete, the others by the worker.
    assert made and sorted(deleted) == sorted(made)
    h.record("8 h deleted mid-flight")
