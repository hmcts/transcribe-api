"""DIAAT-408: a recording over 240 minutes is cut into parts, each stored and submitted as its own Azure
job (stt/preparation_service.py, stt/part_submission.py, stt/splitter.py).

Real ffmpeg on small tone-and-silence audio, with the limits scaled down (Azure's 240 minutes becomes
30 seconds, the part target 22 seconds): the same code at a size a test can afford. Storage is the local
directory backend, the job table is SQLite, Speech is the mock backend (every submission is a distinct
mock job) with ``submit_batch_job`` wrapped to count and to fail on demand.
"""

from __future__ import annotations

import asyncio
import itertools
import wave
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry
from sqlalchemy import update
from sqlmodel import Session

from tests.unit.recording.unit.async_upload_helpers import (
    USER_ID,
    add_job,
    new_engine,
    reload_job,
    tone_and_silence,
)
from transcribe_api.domain import interface_recording as jobs
from transcribe_api.domain.models_recording import JobStatus, SpeechBatchJob, TranscriptionJobPart
from transcribe_api.runtime import settings_dictation, settings_recording
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import configure_telemetry, upload_id_from_blob
from transcribe_api.stt import audio_storage, local_storage, part_submission, recording_limits
from transcribe_api.stt import preparation_service as prep
from transcribe_api.stt.batch_client import BatchSubmissionError
from transcribe_api.stt.part_submission import cleanup_job_parts, delete_job_and_parts, part_blob_name
from transcribe_api.stt.preparation_service import PreparationService

LIMIT = 30.0  # stands for Azure's 240 minutes
TARGET = 22.0  # stands for the 3 h 30 m part target
THREE_PARTS = (("t", 20.0), ("s", 2.0), ("t", 20.0), ("s", 2.0), ("t", 20.0))  # 64 s
FIVE_PARTS = tuple(itertools.chain.from_iterable((("t", 20.0), ("s", 2.0)) for _ in range(5)))  # 110 s


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", "http://api.test")
    monkeypatch.setenv("SPEECH_BACKEND", "mock")
    # Both flags on: the effective limit is MAX_AUDIO_DURATION_SECONDS.
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", "true")
    monkeypatch.setenv("LONG_RECORDINGS_ENABLED", "true")
    settings_recording.get_settings.cache_clear()
    settings_dictation.get_settings.cache_clear()
    settings = get_settings()
    mocker.patch.object(settings, "MAX_AUDIO_DURATION_SECONDS", 1000)
    mocker.patch.object(settings, "PART_TARGET_SECONDS", TARGET)
    mocker.patch.object(settings, "PART_RETRY_BACKOFF_SECONDS", 0)
    mocker.patch.object(settings, "PREPARE_RETRY_BACKOFF_SECONDS", 0)
    mocker.patch.object(prep, "AZURE_BATCH_FILE_LIMIT_SECONDS", LIMIT)
    mocker.patch.object(recording_limits, "AZURE_BATCH_FILE_LIMIT_SECONDS", LIMIT)


@pytest.fixture
def engine(mocker, tmp_path):
    engine = new_engine(tmp_path / "jobs.db", foreign_keys=True)  # as Postgres: a part needs its job
    mocker.patch.object(prep, "get_engine", new=lambda: engine)
    mocker.patch.object(part_submission, "get_engine", new=lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


class Azure:
    """submit_batch_job and delete_batch_job, counting; ``fail`` says which submissions are refused."""

    def __init__(self) -> None:
        self.submitted: list[str] = []  # display names, in order
        self.deleted: list[str] = []
        self.in_flight = 0
        self.peak = 0
        self.fail = lambda name, call: False  # (display name, how many times it was submitted so far)
        self.delay = 0.0
        self.on_submit = None  # an async hook, called with the display name before the answer

    async def submit(self, audio_sas_url, display_name, locale, enable_diarization):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            self.submitted.append(display_name)
            if self.on_submit:
                await self.on_submit(display_name)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail(display_name, self.submitted.count(display_name)):
                raise BatchSubmissionError("Azure said: a secret detail")
            return f"mock://batch-jobs/{uuid4()}"
        finally:
            self.in_flight -= 1

    async def delete(self, job_url):
        self.deleted.append(job_url)


@pytest.fixture
def azure(mocker):
    azure = Azure()
    mocker.patch.object(part_submission, "submit_batch_job", new=azure.submit)
    mocker.patch.object(prep, "delete_batch_job", new=azure.delete)
    mocker.patch.object(part_submission, "delete_batch_job", new=azure.delete)
    return azure


def _waiting(engine, pattern=THREE_PARTS, **fields) -> SpeechBatchJob:
    """A deferred job whose original (a WAV: the service converts it anyway) is in storage."""
    upload_id = uuid4()
    job = add_job(engine, upload_id=upload_id, source_blob_path=f"incoming/{USER_ID}/{upload_id}.wav", **fields)
    local_storage.save(tone_and_silence(pattern), job.source_blob_path)
    return job


async def _prepare(service: PreparationService | None = None) -> PreparationService:
    service = service or PreparationService()
    await service.poll_once()
    await service.wait_idle()
    return service


def _exists(blob_name: str | None) -> bool:
    if blob_name is None:
        return False
    try:
        local_storage.size(blob_name)
    except FileNotFoundError:
        return False
    return True


def _parts(engine, job_id) -> list[TranscriptionJobPart]:
    with Session(engine) as session:
        return jobs.get_job_parts(session, job_id)


def _seconds(blob_name: str) -> float:
    with wave.open(str(local_storage.path_of(blob_name)), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


# The whole path ---------------------------------------------------------------------------------


async def test_a_recording_over_the_limit_is_cut_stored_and_submitted_as_one_azure_job_per_part(engine, azure, tel):
    job = _waiting(engine)
    await _prepare()

    row = reload_job(engine, job.id)
    parts = _parts(engine, job.id)
    # One job row; it is SUBMITTED with no Azure job of its own: each part has its own.
    assert row.status == JobStatus.SUBMITTED and row.batch_job_id is None and row.batch_job_url is None
    assert row.batch_job_status is None and row.error_message is None
    assert [p.part_index for p in parts] == [1, 2, 3] and {p.part_count for p in parts} == {3}
    assert [p.cut_kind for p in parts] == ["start", "silence", "silence"]
    assert all(p.status == "submitted" and p.attempts == 1 and p.batch_job_id for p in parts)
    assert len({p.batch_job_id for p in parts}) == 3  # the mock backend gives every part its own job
    assert sorted(azure.submitted) == [f"batch-{job.id}-part-{i}" for i in (1, 2, 3)]
    # Offsets are cumulative and measured; the parts are the whole recording.
    assert parts[0].offset_seconds == 0.0
    for before, after in itertools.pairwise(parts):
        assert after.offset_seconds == pytest.approx(before.offset_seconds + before.duration_seconds, abs=1e-9)
    assert sum(p.duration_seconds for p in parts) == pytest.approx(64.0, abs=0.01)
    assert max(p.duration_seconds for p in parts) <= LIMIT
    # Every part is stored, and its stored audio is as long as the row says; the full WAV is stored too.
    for part in parts:
        assert _seconds(part.blob_path) == pytest.approx(part.duration_seconds, abs=1e-6)
    assert _seconds(job.audio_blob_path) == pytest.approx(64.0, abs=0.01)
    assert not _exists(job.source_blob_path)
    assert row.audio_duration_seconds == pytest.approx(64.0, abs=0.01)


async def test_part_blobs_are_named_from_the_jobs_own_wav_under_the_users_prefix(engine, azure):
    job = _waiting(engine)
    await _prepare()
    for part in _parts(engine, job.id):
        assert part.blob_path == part_blob_name(job.audio_blob_path, part.part_index)
        assert part.blob_path.startswith(f"uploads/{USER_ID}/")  # the prefix the ownership check trusts
        assert part.blob_path.endswith(f".part{part.part_index:02d}.wav")
        assert upload_id_from_blob(part.blob_path) == upload_id_from_blob(job.audio_blob_path)  # the upload's journey


def test_the_part_name_adds_the_index_before_the_extension():
    assert part_blob_name("uploads/u/9f8e7d6c-1111-2222-3333-444455556666-hearing.wav", 2) == (
        "uploads/u/9f8e7d6c-1111-2222-3333-444455556666-hearing.part02.wav"
    )
    assert part_blob_name("uploads/u/x", 12) == "uploads/u/x.part12.wav"


async def test_the_events_show_one_split_one_submission_per_part_and_one_job_queued(engine, azure, tel):
    job = _waiting(engine)
    await _prepare()

    names = [str(e[c.CUSTOM_EVENT_NAME]) for e in tel.events() if e.get(c.JOURNEY_ID) == str(job.id)]
    assert names.count("preparation.completed") == 1 and names.count("job.created") == 0
    assert names.index("splitting.started") < names.index("splitting.completed") < names.index("job.queued")
    (split,) = tel.events("splitting.completed")
    assert split["split.part_count"] == 3 and split["split.forced_cut_count"] == 0
    assert split["audio.duration_seconds"] == pytest.approx(64.0, abs=0.01)
    done = tel.events("speech_submission.completed")
    assert sorted((e["job.part_index"], e["job.part_count"]) for e in done) == [(1, 3), (2, 3), (3, 3)]
    assert all(e[c.DEPENDENCY_REF] for e in done)
    (queued,) = tel.events("job.queued")  # once for the job, not once per part
    assert queued["job.part_count"] == 3 and queued[c.JOURNEY_ID] == str(job.id)


async def test_a_recording_within_the_limit_takes_the_unchanged_single_file_path_with_no_part_rows(engine, tel, mocker):
    job = _waiting(engine, pattern=(("t", 10.0),))  # 10 s: within the 30 s limit
    submitted = []

    async def single(session, row, **kwargs):
        submitted.append(row.id)
        row.batch_job_id = "single"
        row.status = JobStatus.SUBMITTED
        return jobs.save_job(session, row)

    mocker.patch.object(prep, "submit_existing_job", new=single)
    await _prepare()

    assert _parts(engine, job.id) == [] and submitted == [job.id]
    assert reload_job(engine, job.id).batch_job_id == "single"
    assert tel.events("splitting.started") == []
    assert _exists(job.audio_blob_path)  # the WAV is stored whole, as ever


async def test_the_idempotency_key_stays_on_the_job_and_the_parts_have_none(engine, azure):
    job = _waiting(engine, idempotency_key="key-9")
    await _prepare()
    assert reload_job(engine, job.id).idempotency_key == "key-9"
    assert "idempotency_key" not in TranscriptionJobPart.model_fields


async def test_audio_with_no_silence_is_cut_at_the_ideal_points_and_the_parts_still_add_up(engine, azure, tel):
    job = _waiting(engine, pattern=(("t", 64.0),))
    await _prepare()
    parts = _parts(engine, job.id)
    assert [p.cut_kind for p in parts] == ["start", "forced", "forced"]
    assert sum(p.duration_seconds for p in parts) == pytest.approx(64.0, abs=0.01)
    assert tel.events("splitting.completed")[0]["split.forced_cut_count"] == 2


# The length check ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("async_preparation", "long_recordings"), [("true", "false"), ("false", "true"), ("false", "false")]
)
async def test_with_a_flag_off_a_recording_over_the_limit_still_fails_with_the_sentence(
    engine, azure, monkeypatch, async_preparation, long_recordings
):
    """Only both flags on lift the 240-minute limit: otherwise this is the check that has always been."""
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", async_preparation)
    monkeypatch.setenv("LONG_RECORDINGS_ENABLED", long_recordings)
    settings_dictation.get_settings.cache_clear()
    job = _waiting(engine)  # 64 s against a 30 s limit

    await _prepare()

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED
    assert row.error_message == "The recording is 1 minute long. Recordings can be at most 0 minutes."
    assert _parts(engine, job.id) == [] and azure.submitted == []


async def test_over_the_8_hour_setting_fails_even_with_both_flags_on(engine, azure, mocker):
    mocker.patch.object(get_settings(), "MAX_AUDIO_DURATION_SECONDS", 60)  # 8 hours, scaled down; 64 s is over it
    job = _waiting(engine)
    await _prepare()
    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED and "long" in row.error_message
    assert _parts(engine, job.id) == [] and azure.submitted == []


async def test_the_real_limits_240_minutes_with_the_flags_off_and_8_hours_with_both_on(monkeypatch, mocker):
    """Unscaled: what _check_length enforces (the planner and the check agree on the same constants)."""
    mocker.stopall()  # drop the scaled limits of this module's fixtures
    service = PreparationService()
    monkeypatch.setenv("LONG_RECORDINGS_ENABLED", "false")
    settings_dictation.get_settings.cache_clear()
    with pytest.raises(prep.PreparationFailed) as flags_off:
        await service._check_length(5 * 3600.0)
    assert flags_off.value.message == "The recording is 5 hours long. Recordings can be at most 4 hours."
    await service._check_length(4 * 3600.0)
    monkeypatch.setenv("LONG_RECORDINGS_ENABLED", "true")
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", "true")
    settings_dictation.get_settings.cache_clear()
    await service._check_length(5 * 3600.0)  # accepted: it will be split
    await service._check_length(8 * 3600.0)
    with pytest.raises(prep.PreparationFailed) as too_long:
        await service._check_length(8 * 3600.0 + 120)
    assert too_long.value.message == "The recording is 8 hours 2 minutes long. Recordings can be at most 8 hours."


# Parallelism and order ----------------------------------------------------------------------------


async def test_no_more_than_max_parallel_parts_are_stored_and_submitted_at_once(engine, azure, mocker):
    mocker.patch.object(get_settings(), "MAX_PARALLEL_PARTS", 2)
    job = _waiting(engine, pattern=FIVE_PARTS)
    two_in_flight = asyncio.Event()

    async def hold_until_two_are_in_flight(name):
        # No timing: every submission waits until two are in flight together. If the bound were 1 this never
        # happens (the wait_for fails the test); if it were above 2 a third would get in (peak below).
        if azure.in_flight >= 2:
            two_in_flight.set()
        await asyncio.wait_for(two_in_flight.wait(), 20)

    azure.on_submit = hold_until_two_are_in_flight
    await _prepare()

    assert len(_parts(engine, job.id)) == 5
    assert azure.peak == 2  # bounded, and the bound is used
    assert reload_job(engine, job.id).status == JobStatus.SUBMITTED


async def test_part_1_is_submitted_as_soon_as_its_blob_is_stored_before_later_parts_and_the_full_wav(
    engine, azure, mocker
):
    """Parts are stored and submitted one after the other when only one runs at a time; the full WAV is
    stored beside them and part 1 does not wait for it (this would deadlock if it did)."""
    mocker.patch.object(get_settings(), "MAX_PARALLEL_PARTS", 1)
    job = _waiting(engine)
    log: list[str] = []
    part_1_submitted = asyncio.Event()

    class Recording(audio_storage.LocalAudioStorage):
        async def upload(self, path, blob_name, *, concurrency=None):
            if blob_name == job.audio_blob_path:
                await asyncio.wait_for(part_1_submitted.wait(), 20)  # the full WAV waits for part 1's submission
                log.append("store full")
            else:
                log.append(f"store {blob_name.rsplit('.part', 1)[1][:2]}")
            return await super().upload(path, blob_name, concurrency=concurrency)

    async def on_submit(name):
        log.append(f"submit {name[-1]}")
        if name.endswith("part-1"):
            part_1_submitted.set()

    azure.on_submit = on_submit
    await _prepare(PreparationService(storage=Recording()))

    assert reload_job(engine, job.id).status == JobStatus.SUBMITTED
    parts_log = [entry for entry in log if entry != "store full"]
    assert parts_log == ["store 01", "submit 1", "store 02", "submit 2", "store 03", "submit 3"]
    assert "store full" in log


# Retrying one part --------------------------------------------------------------------------------


async def test_a_part_that_fails_is_retried_alone_and_the_others_are_not_resubmitted(engine, azure, tel):
    azure.fail = lambda name, call: name.endswith("part-2") and call == 1
    job = _waiting(engine)

    await _prepare()

    parts = _parts(engine, job.id)
    assert [p.attempts for p in parts] == [1, 2, 1]
    assert [azure.submitted.count(f"batch-{job.id}-part-{i}") for i in (1, 2, 3)] == [1, 2, 1]
    assert reload_job(engine, job.id).status == JobStatus.SUBMITTED
    (retry,) = tel.events("job.retried")
    assert (
        retry["job.retry.operation"] == "part.submit"
        and retry["job.retry.attempt"] == 1
        and retry["job.part_index"] == 2
    )
    assert [e["job.part_index"] for e in tel.events("speech_submission.failed")] == [2]


async def test_a_part_that_keeps_failing_fails_the_job_with_a_message_naming_the_part_and_cleans_up(engine, azure, tel):
    azure.fail = lambda name, call: name.endswith("part-2")
    job = _waiting(engine, idempotency_key="k")

    await _prepare()

    row = reload_job(engine, job.id)
    parts = _parts(engine, job.id)
    assert row.status == JobStatus.FAILED
    assert (
        row.error_message
        == "Part 2 of 3 of the recording could not be sent for transcription. Please upload the recording again."
    )
    assert "secret" not in row.error_message
    assert row.idempotency_key is None  # released, as for a submission Azure refused
    assert (
        [p.status for p in parts][1] == "failed"
        and parts[1].attempts == 3
        and parts[1].error_message == "speech.submission_rejected"
    )
    # The part blobs and the Azure jobs of the parts that were submitted are gone; the rows stay as the record.
    assert not any(_exists(part_blob_name(job.audio_blob_path, i)) for i in (1, 2, 3))
    assert len(azure.deleted) == sum(1 for p in parts if p.batch_job_id)
    assert all(p.blob_path is None and p.batch_job_url is None for p in parts)
    assert not _exists(job.source_blob_path)
    (finished,) = tel.events("job.finished")
    assert finished[c.ERROR_CODE] == "speech.submission_rejected" and finished[c.JOURNEY_ID] == str(job.id)


async def test_the_part_attempts_limit_is_the_setting(engine, azure, mocker):
    mocker.patch.object(get_settings(), "PART_MAX_ATTEMPTS", 1)
    azure.fail = lambda name, call: name.endswith("part-3")
    job = _waiting(engine)
    await _prepare()
    assert azure.submitted.count(f"batch-{job.id}-part-3") == 1
    assert reload_job(engine, job.id).error_message.startswith("Part 3 of 3 ")


# Interrupted preparation --------------------------------------------------------------------------


async def test_an_interrupted_preparation_resumes_without_resubmitting_the_parts_already_submitted(
    engine, azure, mocker
):
    """The worker stops while part 2 is being submitted (part 1 is with Azure already): the next start
    continues with parts 2 and 3 and submits part 1 no second time."""
    job = _waiting(engine)
    mocker.patch.object(get_settings(), "MAX_PARALLEL_PARTS", 1)  # part 2 is in flight while 3 waits
    part_2_in_flight = asyncio.Event()

    async def block_part_2(name):
        if name.endswith("part-2"):
            part_2_in_flight.set()
            await asyncio.Event().wait()

    azure.on_submit = block_part_2
    service = PreparationService()
    await service.poll_once()
    await asyncio.wait_for(part_2_in_flight.wait(), 20)
    for task in list(service._running):
        task.cancel()  # the worker is stopped
    await asyncio.gather(*service._running, return_exceptions=True)

    first = _parts(engine, job.id)
    assert [bool(p.batch_job_id) for p in first] == [True, False, False]
    assert reload_job(engine, job.id).status == JobStatus.PENDING
    recorded = [(p.offset_seconds, p.duration_seconds) for p in first]

    azure.on_submit = None
    await _prepare(PreparationService())

    after = _parts(engine, job.id)
    assert reload_job(engine, job.id).status == JobStatus.SUBMITTED
    assert all(p.batch_job_id for p in after)
    assert after[0].batch_job_id == first[0].batch_job_id  # part 1 was left alone
    assert azure.submitted.count(f"batch-{job.id}-part-1") == 1
    assert [azure.submitted.count(f"batch-{job.id}-part-{i}") for i in (2, 3)] == [2, 1]  # part 2 was in flight twice
    # The parts are the same parts: the recording was cut again at the recorded offsets.
    assert [(p.offset_seconds, p.duration_seconds) for p in after] == recorded
    assert [p.attempts for p in after] == [1, 2, 1]


async def test_a_job_whose_parts_were_all_submitted_before_the_worker_died_is_settled_not_resubmitted(engine, azure):
    job = _waiting(engine)
    await _prepare()
    submitted_before = list(azure.submitted)
    # Back to the state of a worker that died after the last part, before the job was marked SUBMITTED.
    with Session(engine) as session:
        session.execute(
            update(SpeechBatchJob)
            .where(SpeechBatchJob.id == job.id)
            .values(
                status=JobStatus.PENDING,
                preparation_attempts=1,
                updated_datetime=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        session.commit()
    local_storage.save(tone_and_silence(THREE_PARTS), job.source_blob_path)

    await _prepare()

    assert azure.submitted == submitted_before  # nothing submitted again
    assert reload_job(engine, job.id).status == JobStatus.SUBMITTED


async def test_a_recording_that_does_not_cut_into_the_recorded_parts_is_not_resumed_onto_them(engine, azure, tel):
    job = _waiting(engine)
    await _prepare()
    with Session(engine) as session:  # the parts were recorded for audio 5 s longer than this
        part = jobs.get_job_parts(session, job.id)[0]
        session.execute(
            update(TranscriptionJobPart)
            .where(TranscriptionJobPart.id == part.id)
            .values(duration_seconds=part.duration_seconds + 5)
        )
        session.execute(
            update(SpeechBatchJob)
            .where(SpeechBatchJob.id == job.id)
            .values(
                status=JobStatus.PENDING,
                preparation_attempts=1,
                updated_datetime=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        session.commit()
    local_storage.save(tone_and_silence(THREE_PARTS), job.source_blob_path)

    await _prepare()

    assert reload_job(engine, job.id).status == JobStatus.PENDING  # retried, not submitted onto a different cut
    assert tel.events("splitting.failed")[-1][c.ERROR_CODE] == "preparation.split_mismatch"


async def test_a_split_that_fails_is_retried_then_fails_the_job_and_cleans_up(engine, azure, mocker, tel):
    from transcribe_api.stt.splitter import SplitError

    mocker.patch.object(prep, "split_wav", side_effect=SplitError("ffmpeg segment failed"))
    job = _waiting(engine)
    for _ in range(3):
        await _prepare()
    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED and row.preparation_attempts == 3
    assert azure.submitted == [] and _parts(engine, job.id) == []
    assert {e[c.ERROR_CODE] for e in tel.events("splitting.failed")} == {"split.failed"}


# A job deleted while its parts are in flight --------------------------------------------------------


async def test_a_job_deleted_while_a_part_is_being_submitted_leaves_no_azure_job_or_blob(engine, azure, mocker):
    job = _waiting(engine)
    mocker.patch.object(get_settings(), "MAX_PARALLEL_PARTS", 1)

    async def delete_the_job_during_part_2(name):
        if name.endswith("part-2"):
            assert await delete_job_and_parts(job.id, engine=engine)  # what DELETE /jobs/{id} does
            local_storage.delete(job.audio_blob_path)

    azure.on_submit = delete_the_job_during_part_2
    await _prepare()

    assert reload_job(engine, job.id) is None and _parts(engine, job.id) == []
    # Part 1's Azure job was deleted by the delete; part 2's, made after it, by the worker: none is left.
    assert len(azure.deleted) == 2
    leftovers = [
        name
        for name in (
            job.audio_blob_path,
            *(part_blob_name(job.audio_blob_path, i) for i in (1, 2, 3)),
            job.source_blob_path,
        )
        if _exists(name)
    ]
    assert leftovers == []
    assert azure.submitted == [f"batch-{job.id}-part-1", f"batch-{job.id}-part-2"]  # part 3 was never started


async def test_a_job_deleted_before_its_parts_are_recorded_is_gone_not_a_failure_to_retry(engine, azure, mocker, tel):
    """The window the delete's cleanup used to miss: the worker has cut the recording and is about to record
    the parts when the job is deleted. Inserting a part then breaks its foreign key; the worker must read that
    as "the job is gone" (nothing submitted, nothing left, no retry), not as a storage failure."""
    job = _waiting(engine)
    real_record = PreparationService._record_parts

    def delete_then_record(self, claimed, prepared):
        assert asyncio.run(delete_job_and_parts(claimed.id, engine=engine))  # what DELETE /jobs/{id} does
        return real_record(self, claimed, prepared)

    mocker.patch.object(PreparationService, "_record_parts", delete_then_record)
    retried = mocker.spy(PreparationService, "_retry_or_fail")

    await _prepare()

    retried.assert_not_called()
    assert reload_job(engine, job.id) is None and _parts(engine, job.id) == []
    assert azure.submitted == [] and azure.deleted == []
    assert not any(
        _exists(n)
        for n in (
            job.source_blob_path,
            job.audio_blob_path,
            *(part_blob_name(job.audio_blob_path, i) for i in (1, 2, 3)),
        )
    )


async def test_parts_recorded_by_the_worker_after_the_deletes_cleanup_are_released_and_the_delete_finishes(
    engine, azure, mocker
):
    """Cleanup ran before the worker created its part rows; the worker then creates, stores and submits a part.
    The last step of the delete sees the part (it locks the job and reads the rows in one transaction), deletes
    what it recorded and tries again, instead of cascading the row away with its blob and Azure job unseen."""
    job = _waiting(engine)
    real_cleanup = part_submission.cleanup_job_parts
    made = []

    async def cleanup_then_the_worker_records_a_part(*args, **kwargs):
        clean = await real_cleanup(*args, **kwargs)
        plan = jobs.PartPlan(1, 1, 0.0, 10.0, "start", part_blob_name(job.audio_blob_path, 1))
        with Session(engine) as session:
            (row,) = jobs.create_job_parts(session, job.id, [plan])
            local_storage.save(b"part", row.blob_path)
            url = f"mock://batch-jobs/{uuid4()}"
            made.append(url)
            jobs.mark_part_submitted(session, row.id, "azure-1", url)
        return clean

    mocker.patch.object(part_submission, "cleanup_job_parts", cleanup_then_the_worker_records_a_part)
    # Only the first cleanup of the delete is followed by a recording; the second finds nothing new.
    assert await delete_job_and_parts(job.id, engine=engine) is True

    assert reload_job(engine, job.id) is None and _parts(engine, job.id) == []
    assert azure.deleted == made and len(made) == 1
    assert not _exists(part_blob_name(job.audio_blob_path, 1))


async def test_a_worker_that_keeps_recording_parts_leaves_the_job_and_its_rows_for_a_retry(engine, azure, mocker):
    job = _waiting(engine)
    plan = jobs.PartPlan(1, 1, 0.0, 10.0, "start", part_blob_name(job.audio_blob_path, 1))
    with Session(engine) as session:
        (row,) = jobs.create_job_parts(session, job.id, [plan])
    real_last_step = jobs.delete_job_unless_parts_unreleased
    recorded = []

    def the_worker_records_before_each_last_step(session, job_id):
        url = f"mock://batch-jobs/{uuid4()}"
        recorded.append(url)
        with Session(engine) as other:
            jobs.mark_part_submitted(other, row.id, "azure-1", url)
        return real_last_step(session, job_id)

    mocker.patch.object(jobs, "delete_job_unless_parts_unreleased", the_worker_records_before_each_last_step)

    assert await delete_job_and_parts(job.id, engine=engine) is False

    assert len(recorded) == part_submission.DELETE_ATTEMPTS
    assert azure.deleted == recorded  # every Azure job the worker made was deleted, however often
    assert reload_job(engine, job.id) is not None and len(_parts(engine, job.id)) == 1  # nothing was lost

    mocker.patch.object(jobs, "delete_job_unless_parts_unreleased", real_last_step)
    assert await delete_job_and_parts(job.id, engine=engine) is True
    assert reload_job(engine, job.id) is None


async def test_an_azure_job_recorded_while_the_cleanup_deletes_the_blob_is_not_forgotten_and_is_deleted(
    engine, azure, mocker
):
    """The worker accepts a part and records its Azure job while the delete awaits the deletion of that part's
    blob. Forgetting the part by its id alone would erase the new URL, and the last step would then delete the row
    and leave the Azure job running. Each field is forgotten only if it still holds what was seen."""
    job = _waiting(engine)
    plan = jobs.PartPlan(1, 1, 0.0, 10.0, "start", part_blob_name(job.audio_blob_path, 1))
    with Session(engine) as session:
        (row,) = jobs.create_job_parts(session, job.id, [plan])
    local_storage.save(b"part", row.blob_path)  # what the worker stored; its Azure job is not recorded yet
    url = f"mock://batch-jobs/{uuid4()}"
    real_delete = audio_storage.LocalAudioStorage.delete
    recorded = []

    async def delete_and_meanwhile_the_worker_records_the_job(self, blob_name):
        await real_delete(self, blob_name)
        if not recorded:
            recorded.append(url)
            with Session(engine) as session:
                assert jobs.mark_part_submitted(session, row.id, "azure-1", url)

    mocker.patch.object(audio_storage.LocalAudioStorage, "delete", delete_and_meanwhile_the_worker_records_the_job)

    assert await cleanup_job_parts(job.id, engine=engine) is True
    (kept,) = _parts(engine, job.id)
    assert kept.batch_job_url == url and kept.blob_path is None  # the blob was seen and deleted; the URL was not seen
    assert azure.deleted == []

    assert await delete_job_and_parts(job.id, engine=engine) is True
    assert azure.deleted == [url]  # the Azure job the worker made is deleted, not left running
    assert reload_job(engine, job.id) is None and _parts(engine, job.id) == []
    assert not _exists(part_blob_name(job.audio_blob_path, 1))


async def test_cleanup_deletes_every_part_blob_and_azure_job_and_can_keep_or_remove_the_rows(engine, azure):
    job = _waiting(engine)
    await _prepare()
    parts = _parts(engine, job.id)
    assert all(_exists(p.blob_path) for p in parts)

    assert await cleanup_job_parts(job.id, engine=engine) is True  # a failed job's cleanup: rows stay
    assert not any(_exists(p.blob_path) for p in parts) and len(azure.deleted) == 3
    kept = _parts(engine, job.id)
    assert len(kept) == 3 and all(p.blob_path is None and p.batch_job_url is None for p in kept)

    assert await delete_job_and_parts(job.id, engine=engine) is True
    assert _parts(engine, job.id) == [] and reload_job(engine, job.id) is None


async def test_a_cleanup_that_cannot_delete_everything_says_so_and_marks_the_job(engine, azure, mocker):
    job = _waiting(engine)
    await _prepare()

    async def refuse(url):
        raise RuntimeError("azure down")

    mocker.patch.object(part_submission, "delete_batch_job", new=refuse)
    assert await cleanup_job_parts(job.id, engine=engine) is False
    row = reload_job(engine, job.id)
    assert row.needs_cleanup is True and row.cleanup_failure_reason
    assert all(p.batch_job_url for p in _parts(engine, job.id))  # still recorded: a later cleanup can try again


@pytest.mark.parametrize("recorded_before_the_delete", [0, 1, 2])
async def test_parts_accepted_by_azure_when_the_job_is_deleted_are_not_left_as_azure_jobs(
    engine, azure, mocker, recorded_before_the_delete
):
    """The window, made to happen every time with events and no sleeps: all three parts are in flight at once
    and accepted by Azure, none has been recorded yet; then ``recorded_before_the_delete`` of them are recorded,
    the job is deleted (as DELETE /jobs/{id} does), and the rest try to record. Every Azure job made is deleted
    (those recorded before the delete by the delete, the others by the worker that finds its row gone), and
    no blob, row or job is left."""
    import threading

    mocker.patch.object(get_settings(), "MAX_PARALLEL_PARTS", 4)
    job = _waiting(engine)
    loop = asyncio.get_running_loop()
    in_flight, accepted_all = set(), asyncio.Event()
    arrived, all_arrived = [], asyncio.Event()
    recorded_one = asyncio.Event()
    permits = threading.Semaphore(0)
    made: list[str] = []
    real_submit = azure.submit

    async def hold_until_all_three_are_in_flight(name):
        in_flight.add(name)
        if len(in_flight) == 3:
            accepted_all.set()
        await asyncio.wait_for(accepted_all.wait(), 20)

    async def submit(audio_sas_url, display_name, locale, enable_diarization):
        url = await real_submit(audio_sas_url, display_name, locale, enable_diarization)
        made.append(url)
        return url

    real_record = PreparationService._record_part_submitted
    recorded = []

    def record(self, part_id, batch_job_id, batch_job_url):
        arrived.append(part_id)
        if len(arrived) == 3:
            loop.call_soon_threadsafe(all_arrived.set)  # all three are accepted and about to be recorded
        permits.acquire()  # held until the test lets this one through
        done = real_record(self, part_id, batch_job_id, batch_job_url)
        recorded.append(done)
        if len(recorded) == recorded_before_the_delete:
            loop.call_soon_threadsafe(recorded_one.set)
        return done

    async def the_delete():
        await asyncio.wait_for(all_arrived.wait(), 20)
        for _ in range(recorded_before_the_delete):
            permits.release()
        if recorded_before_the_delete:
            await asyncio.wait_for(recorded_one.wait(), 20)
        assert await delete_job_and_parts(job.id, engine=engine)  # what DELETE /jobs/{id} does
        for _ in range(3 - recorded_before_the_delete):
            permits.release()

    azure.on_submit = hold_until_all_three_are_in_flight
    mocker.patch.object(part_submission, "submit_batch_job", new=submit)
    mocker.patch.object(PreparationService, "_record_part_submitted", record)

    deleting = asyncio.create_task(the_delete())
    await _prepare()
    await asyncio.wait_for(deleting, 20)

    assert reload_job(engine, job.id) is None and _parts(engine, job.id) == []
    assert len(made) == 3 and len(set(azure.deleted)) == 3 and sorted(azure.deleted) == sorted(made)
    assert (
        recorded.count(True) == recorded_before_the_delete
    )  # the parts recorded after the delete found nothing to record on
    assert not any(
        _exists(n) for n in (job.audio_blob_path, *(part_blob_name(job.audio_blob_path, i) for i in (1, 2, 3)))
    )
