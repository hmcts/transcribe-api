"""DIAAT-408: polling a recording in parts, assembling its transcript, and cleaning up
(stt/polling_service.py, stt/part_merge.py, domain/interface_recording.py).

The job has no Azure job of its own: it is polled through its part rows, moves to RUNNING when a part
runs, is assembled exactly once when every part has succeeded, and fails at once when a part fails.
The same scaled-down setup as the preparation tests (30 s stands for Azure's 240 minutes); jobs are
prepared by the real PreparationService with the mock Speech backend, and polled by the real
BatchPollingService over a SQLite file database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry
from sqlalchemy import update
from sqlmodel import Session

from tests.unit.recording.unit.async_upload_helpers import new_engine, reload_job
from tests.unit.recording.unit.audio.multipart_helpers import prepare as prepare_job
from tests.unit.recording.unit.audio.multipart_helpers import scale_down, use_engine, waiting
from transcribe_api.domain import interface_recording as jobs
from transcribe_api.domain.models_recording import BatchJobStatus, JobStatus, SpeechBatchJob, TranscriptionJobPart
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import configure_telemetry
from transcribe_api.stt import local_storage, part_submission, polling_service
from transcribe_api.stt.batch_client import BatchResults, BatchResultsInvalidError
from transcribe_api.stt.polling_service import BatchPollingService


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch, mocker):
    scale_down(monkeypatch, mocker, tmp_path)


@pytest.fixture
def engine(mocker, tmp_path):
    engine = new_engine(tmp_path / "jobs.db")
    use_engine(mocker, engine)
    yield engine
    engine.dispose()


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


class Speech:
    """What Azure says about each part's job (by part number): Succeeded unless the test says otherwise."""

    def __init__(self) -> None:
        self.status: dict[int, str] = {}
        self.deleted: list[str] = []

    async def get_status(self, url: str) -> dict:
        part = int(url.rsplit("-part-", 1)[1]) if "-part-" in url else 0  # 0: a single-file job
        status = self.status.get(part, "Succeeded")
        now = datetime.now(UTC)
        data = {
            "status": status,
            "createdDateTime": (now - timedelta(minutes=5)).isoformat(),
            "lastActionDateTime": now.isoformat(),
        }
        if status == "Failed":
            data["properties"] = {"error": {"code": "InvalidData", "message": "Azure said: a secret detail"}}
        return data

    async def delete(self, url: str) -> None:
        self.deleted.append(url)


@pytest.fixture
def speech(mocker):
    speech = Speech()
    mocker.patch.object(polling_service, "get_batch_job_status", new=speech.get_status)
    mocker.patch.object(part_submission, "delete_batch_job", new=speech.delete)
    mocker.patch.object(polling_service, "AsyncAzureBlobManager", return_value=_blob_manager(mocker))
    return speech


def _blob_manager(mocker):
    manager = mocker.AsyncMock()
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    return manager


def _poller() -> BatchPollingService:
    service = BatchPollingService(webhook_secret_resolver=lambda _caller: "")
    service._dispatch_success = AsyncMock()
    service._dispatch_failure = AsyncMock()
    return service


async def _prepared(engine, pattern=None, **fields) -> SpeechBatchJob:
    job = waiting(engine, **({"pattern": pattern} if pattern else {}), **fields)
    await prepare_job()
    assert reload_job(engine, job.id).status == JobStatus.SUBMITTED
    return job


def _parts(engine, job_id) -> list[TranscriptionJobPart]:
    with Session(engine) as session:
        return jobs.get_job_parts(session, job_id)


def _exists(name: str | None) -> bool:
    if not name:
        return False
    try:
        local_storage.size(name)
    except FileNotFoundError:
        return False
    return True


# Running ----------------------------------------------------------------------------------------


async def test_the_job_runs_when_a_part_runs_and_is_not_assembled_until_every_part_has_succeeded(engine, speech, tel):
    job = await _prepared(engine)
    speech.status = {1: "Running", 2: "NotStarted", 3: "NotStarted"}
    poller = _poller()

    await poller._poll_parts_once()

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.RUNNING and row.processing_started_datetime is not None
    assert [p.status for p in _parts(engine, job.id)] == ["running", "submitted", "submitted"]
    assert len(tel.events("speech.job_running")) == 1  # the queue wait ended once, for the job

    speech.status = {1: "Running", 2: "Running", 3: "Succeeded"}
    await poller._poll_parts_once()
    assert len(tel.events("speech.job_running")) == 1  # not again for the second part
    assert reload_job(engine, job.id).status == JobStatus.RUNNING
    assert [p.status for p in _parts(engine, job.id)] == ["running", "running", "succeeded"]
    assert reload_job(engine, job.id).dialogue_entries == []  # nothing assembled yet


async def test_a_job_whose_parts_all_succeed_between_two_polls_is_assembled_without_running(engine, speech):
    job = await _prepared(engine)
    await _poller()._poll_parts_once()  # the mock backend answers Succeeded at once
    assert reload_job(engine, job.id).status == JobStatus.SUCCEEDED


# The merge ----------------------------------------------------------------------------------------


async def test_the_transcript_is_the_parts_in_order_with_every_time_moved_by_the_parts_offset(engine, speech):
    job = await _prepared(engine)
    parts = _parts(engine, job.id)
    offsets = [p.offset_seconds for p in parts]
    assert offsets[0] == 0.0 and offsets[1] > 0 and offsets[2] > offsets[1]

    await _poller()._poll_parts_once()

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.SUCCEEDED and row.batch_job_status == BatchJobStatus.SUCCEEDED
    entries = row.dialogue_entries
    assert len(entries) == 6  # two speakers in each of the 3 parts: counted, none lost, none merged across parts
    for k, offset in enumerate(offsets):
        first, second = entries[2 * k], entries[2 * k + 1]
        assert first["text"] == f"Part {k + 1} opens with speaker one."
        assert second["text"] == f"Part {k + 1} answers with speaker two."
        # The mock transcript of each part starts at 0 (its first word) and at 4 s (the second speaker):
        # in the recording they start at the part's own offset plus that.
        assert first["start_time"] == pytest.approx(offset + 0.0)
        assert first["words"][0]["start_time"] == pytest.approx(offset + 0.0)
        assert first["words"][1]["start_time"] == pytest.approx(offset + 0.5)
        assert first["end_time"] == pytest.approx(offset + 2.9)  # 5 words in: 2.5 + 0.4
        assert second["start_time"] == pytest.approx(offset + 4.0)
        assert second["words"][-1]["end_time"] == pytest.approx(offset + 4.0 + 2.5 + 0.4)
        assert len(first["words"]) == len(second["words"]) == 6
    starts = [e["start_time"] for e in entries]
    assert starts == sorted(starts) and len(set(starts)) == 6  # monotonic across every join
    word_starts = [w["start_time"] for e in entries for w in e["words"]]
    assert word_starts == sorted(word_starts) and len(word_starts) == 36
    # Part 2's first word is at the offset of part 2 plus its own time (0 here), not at 0.
    assert entries[2]["words"][0]["start_time"] == pytest.approx(offsets[1])


async def test_confidence_and_accuracy_are_those_of_the_whole_transcript(engine, speech):
    from transcribe_api.domain.models_recording import DialogueEntry
    from transcribe_api.stt.accuracy import compute_accuracy

    job = await _prepared(engine)
    await _poller()._poll_parts_once()
    entries = [DialogueEntry(**e) for e in reload_job(engine, job.id).dialogue_entries]
    accuracy = compute_accuracy(entries)
    assert accuracy.words_transcribed == 36 and accuracy.confidence_score == pytest.approx(90.0)


async def test_run_metadata_and_provenance_describe_the_job_not_one_part(engine, speech):
    job = await _prepared(engine)
    await _poller()._poll_parts_once()
    row = reload_job(engine, job.id)
    assert row.model_identifier and row.transcription_duration_seconds == pytest.approx(
        300.0, abs=5
    )  # the 5 min the mock says each ran
    assert row.provenance["speech"]["azure_job_id"] == _parts(engine, job.id)[0].batch_job_id
    assert row.audio_duration_seconds == pytest.approx(64.0, abs=0.01)  # the recording's, unchanged


async def test_the_audio_duration_of_the_job_is_the_recordings_and_agrees_with_the_sum_of_its_parts(engine, speech):
    job = await _prepared(engine)
    parts = _parts(engine, job.id)
    assert sum(p.duration_seconds for p in parts) == pytest.approx(
        reload_job(engine, job.id).audio_duration_seconds, abs=0.01
    )


async def test_parts_that_disagree_with_the_recording_are_logged_and_the_recording_length_kept(engine, speech, caplog):
    job = await _prepared(engine)
    with Session(engine) as session:
        session.execute(update(SpeechBatchJob).where(SpeechBatchJob.id == job.id).values(audio_duration_seconds=90.0))
        session.commit()
    await _poller()._poll_parts_once()
    row = reload_job(engine, job.id)
    assert row.status == JobStatus.SUCCEEDED and row.audio_duration_seconds == 90.0
    assert "add up to" in caplog.text


# Billing --------------------------------------------------------------------------------------------


def test_billed_duration_is_the_sum_of_what_azure_reported_for_each_part():
    from transcribe_api.stt.part_merge import PartResult, billed_seconds

    def part(i, azure):
        return PartResult(i, 3, 0.0, 100.0, [], azure)

    assert billed_seconds([part(1, 9_600.5), part(2, 9_599.5), part(3, 9_000.0)]) == (28_200.0, "azure")
    # A part Azure gave no duration for counts at the length it was cut to, and the source says so.
    assert billed_seconds([part(1, 9_600.0), part(2, None)]) == (9_700.0, "estimate")
    assert billed_seconds([part(1, None)]) == (100.0, "estimate")


async def test_the_speech_usage_recorded_for_a_job_in_parts_is_the_sum_of_its_parts_once(engine, speech, tel, mocker):
    await _prepared(engine)
    mocker.patch.object(get_settings(), "SPEECH_BACKEND", "azure")  # the mock backend records no usage
    durations = {1: 100.0, 2: 200.0, 3: 50.0}

    async def results(url, transcription_job_id=None):
        part = int(url.rsplit("-part-", 1)[1])
        from transcribe_api.stt.batch_client import _mock_part_entries

        return BatchResults(_mock_part_entries(part), durations[part])

    mocker.patch.object(polling_service, "get_batch_results", new=results)
    await _poller()._poll_parts_once()

    (point,) = [p for p in tel.metric_points("hmcts.speech.audio.duration")]
    assert point["value"] == 350.0
    (finished,) = tel.events("job.finished")
    assert finished["speech.audio_duration_s"] == 350.0 and finished["speech.audio_duration_source"] == "azure"
    assert finished["job.part_count"] == 3 and finished[c.OUTCOME] == "success"


# Exactly once ---------------------------------------------------------------------------------------


async def test_two_pollers_assembling_the_same_job_at_the_same_moment_finish_it_once(engine, speech, tel):
    job = await _prepared(engine)
    callback_one, callback_two = _poller(), _poller()

    await asyncio.gather(callback_one._assemble(job.id), callback_two._assemble(job.id))

    assert reload_job(engine, job.id).status == JobStatus.SUCCEEDED
    assert len(tel.events("job.finished")) == 1 and len(tel.events("transcript.available")) == 1
    assert callback_one._dispatch_success.await_count + callback_two._dispatch_success.await_count == 1
    assert len(speech.deleted) == 3  # each Azure job deleted once, by the one that finished the job
    assert len(reload_job(engine, job.id).dialogue_entries) == 6


async def test_a_job_assembled_again_after_it_finished_changes_nothing_and_says_nothing(engine, speech, tel):
    job = await _prepared(engine)
    poller = _poller()
    await poller._poll_parts_once()
    before = reload_job(engine, job.id).dialogue_entries

    await poller._assemble(job.id)  # as a restarted worker, or a second poller, would

    assert reload_job(engine, job.id).dialogue_entries == before
    assert len(tel.events("job.finished")) == 1 and poller._dispatch_success.await_count == 1


async def test_a_worker_that_dies_before_the_transcript_is_saved_leaves_nothing_half_assembled(
    engine, speech, tel, mocker
):
    job = await _prepared(engine)
    poller = _poller()
    killed = mocker.patch.object(poller, "_complete", side_effect=ConnectionError("worker killed"))

    with pytest.raises(ConnectionError):
        await poller._assemble(job.id)

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.SUBMITTED and row.dialogue_entries == [] and row.provenance is None
    assert tel.events("job.finished") == [] and len(speech.deleted) == 0  # nothing visible, nothing deleted

    mocker.stop(killed)  # the restarted worker
    await poller._poll_parts_once()
    assert reload_job(engine, job.id).status == JobStatus.SUCCEEDED


async def test_a_worker_that_dies_after_the_save_leaves_a_finished_job_and_the_sweep_deletes_the_leftovers(
    engine, speech, tel, mocker
):
    job = await _prepared(engine)
    poller = _poller()
    real_cleanup = polling_service.cleanup_job_parts
    mocker.patch.object(polling_service, "cleanup_job_parts", new=AsyncMock(side_effect=RuntimeError("killed")))
    await poller._assemble(job.id)  # saved; its cleanup never happened

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.SUCCEEDED and len(row.dialogue_entries) == 6
    assert any(p.blob_path and _exists(p.blob_path) for p in _parts(engine, job.id))

    mocker.patch.object(polling_service, "cleanup_job_parts", new=real_cleanup)
    await _poller()._poll_parts_once()  # the next poll sweeps

    assert not any(_exists(p.blob_path) for p in _parts(engine, job.id)) and len(speech.deleted) == 3
    assert all(p.blob_path is None and p.batch_job_url is None for p in _parts(engine, job.id))
    assert len(tel.events("job.finished")) == 1  # the sweep does not finish the job again


# A part fails -------------------------------------------------------------------------------------


async def test_a_part_azure_reports_failed_fails_the_job_at_once_and_cleans_up_the_others(engine, speech, tel):
    job = await _prepared(engine, idempotency_key="k")
    speech.status = {1: "Running", 2: "Failed", 3: "Running"}
    poller = _poller()

    await poller._poll_parts_once()

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED and row.batch_job_status == BatchJobStatus.FAILED
    assert (
        row.error_message == "Part 2 of 3 of the recording could not be transcribed. Please upload the recording again."
    )
    assert "secret" not in row.error_message and row.dialogue_entries == []
    parts = _parts(engine, job.id)
    assert parts[1].status == "failed" and parts[1].error_message == "speech.job_failed"
    # The Azure jobs and the blobs of every part are deleted; the rows stay as the record.
    assert len(speech.deleted) == 3 and not any(_exists(p.blob_path) for p in parts)
    assert all(p.blob_path is None and p.batch_job_url is None for p in parts)
    (finished,) = tel.events("job.finished")
    assert (
        finished[c.ERROR_CODE] == "speech.job_failed"
        and finished[c.OUTCOME] == "error"
        and finished["job.part_count"] == 3
    )
    (failed_in_speech,) = tel.events("speech.job_failed")
    assert failed_in_speech["job.part_index"] == 2 and failed_in_speech["speech.error_code"] == "InvalidData"
    poller._dispatch_failure.assert_awaited_once()
    assert _exists(job.audio_blob_path)  # the recording itself is kept, as for a single-file job


async def test_a_late_result_cannot_bring_a_failed_job_back(engine, speech, tel):
    job = await _prepared(engine)
    speech.status = {1: "Succeeded", 2: "Failed", 3: "Running"}
    poller = _poller()
    await poller._poll_parts_once()
    assert reload_job(engine, job.id).status == JobStatus.FAILED

    # Part 3 then finishes, and a poller that read the parts before the failure assembles the job.
    with Session(engine) as session:
        session.execute(
            update(TranscriptionJobPart).where(TranscriptionJobPart.job_id == job.id).values(status="succeeded")
        )
        session.commit()
    speech.status = {}
    await poller._poll_parts_once()
    await poller._assemble(job.id)

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED and row.dialogue_entries == []
    assert len(tel.events("job.finished")) == 1 and tel.events("transcript.available") == []


async def test_parts_of_a_failed_job_are_not_polled(engine, speech):
    job = await _prepared(engine)
    with Session(engine) as session:
        session.execute(update(SpeechBatchJob).where(SpeechBatchJob.id == job.id).values(status=JobStatus.FAILED))
        session.commit()
        assert jobs.fetch_pending_parts(session) == []
        assert (
            jobs.record_part_polled(session, _parts(engine, job.id)[0].id, status="succeeded") is False
        )  # a late answer is ignored


async def test_results_that_cannot_be_retrieved_fail_the_job_naming_the_part(engine, speech, tel, mocker):
    job = await _prepared(engine)

    async def results(url, transcription_job_id=None):
        if url.endswith("-part-2"):
            raise BatchResultsInvalidError("Malformed batch results")
        from transcribe_api.stt.batch_client import _mock_part_entries

        return BatchResults(_mock_part_entries(int(url.rsplit("-part-", 1)[1])), None)

    mocker.patch.object(polling_service, "get_batch_results", new=results)
    await _poller()._poll_parts_once()

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.FAILED and row.dialogue_entries == []
    assert (
        row.error_message
        == "The transcript of part 2 of 3 of the recording could not be retrieved. Please upload the recording again."
    )
    (finished,) = tel.events("job.finished")
    assert finished[c.ERROR_CODE] == "speech.results_invalid"
    assert len(speech.deleted) == 3
    assert [e["job.part_index"] for e in tel.events("results_retrieval.failed")] == [2]


async def test_a_poll_that_cannot_reach_azure_leaves_the_part_for_the_next_poll(engine, speech, tel, mocker):
    job = await _prepared(engine)

    async def unreachable(url):
        raise ConnectionError("network down")

    mocker.patch.object(polling_service, "get_batch_job_status", new=unreachable)
    await _poller()._poll_parts_once()
    assert reload_job(engine, job.id).status == JobStatus.SUBMITTED
    assert len(tel.events("job.poll_failed")) == 3 and {e["job.part_index"] for e in tel.events("job.poll_failed")} == {
        1,
        2,
        3,
    }


# Cleanup --------------------------------------------------------------------------------------------


async def test_completion_deletes_every_part_blob_and_azure_job_and_keeps_the_rows(engine, speech):
    job = await _prepared(engine)
    blobs = [p.blob_path for p in _parts(engine, job.id)]
    assert all(_exists(b) for b in blobs)

    await _poller()._poll_parts_once()

    assert len(speech.deleted) == 3 and not any(_exists(b) for b in blobs)
    parts = _parts(engine, job.id)
    assert len(parts) == 3 and all(p.blob_path is None and p.batch_job_url is None for p in parts)
    assert _exists(job.audio_blob_path)  # the full WAV is for playback: it stays


async def test_a_cleanup_that_fails_does_not_fail_the_job_and_is_tried_again(engine, speech, mocker):
    job = await _prepared(engine)

    async def refuse(url):
        raise RuntimeError("azure down")

    mocker.patch.object(part_submission, "delete_batch_job", new=refuse)
    await _poller()._poll_parts_once()

    row = reload_job(engine, job.id)
    assert row.status == JobStatus.SUCCEEDED and len(row.dialogue_entries) == 6  # the transcript is safe
    assert row.needs_cleanup is True and row.cleanup_failure_reason
    assert all(p.batch_job_url for p in _parts(engine, job.id))  # still recorded

    mocker.patch.object(part_submission, "delete_batch_job", new=speech.delete)
    await _poller()._poll_parts_once()  # Azure is back
    row = reload_job(engine, job.id)
    assert row.needs_cleanup is False and row.cleanup_failure_reason is None
    assert len(speech.deleted) == 3 and all(p.batch_job_url is None for p in _parts(engine, job.id))


# Selection ------------------------------------------------------------------------------------------


async def test_many_jobs_in_parts_are_polled_fairly_and_never_starve_a_single_file_job(engine, speech, mocker):
    mocker.patch.object(get_settings(), "MAX_PARALLEL_PARTS", 4)
    ids = []
    for _ in range(4):  # 12 parts: more than one batch of 10
        ids.append((await _prepared(engine)).id)
    from tests.unit.recording.unit.async_upload_helpers import add_job

    single_job = add_job(
        engine,
        source_blob_path=None,
        status=JobStatus.SUBMITTED,
        batch_job_id="b",
        batch_job_url="mock://batch-jobs/x",
        batch_job_status=BatchJobStatus.NOT_STARTED,
    )
    with Session(engine) as session:
        first = jobs.fetch_pending_parts(session)
        assert len(first) == 10
        for part in first:
            jobs.record_part_polled(session, part.id)  # polled: behind the others now
        second = jobs.fetch_pending_parts(session)
        assert {p.id for p in second} >= {p.id for p in _all_parts(session, ids)} - {p.id for p in first}
        # The single-file query is its own: jobs in parts have no batch_job_status, so they never fill it.
        assert [j.id for j in jobs.fetch_pending_batch_jobs(session)] == [single_job.id]


def _all_parts(session, job_ids):
    return [p for job_id in job_ids for p in jobs.get_job_parts(session, job_id)]


async def test_a_single_file_job_in_the_same_poll_is_unaffected(engine, speech, mocker):
    from tests.unit.recording.unit.async_upload_helpers import add_job

    job = await _prepared(engine)
    single = add_job(
        engine,
        source_blob_path=None,
        status=JobStatus.SUBMITTED,
        batch_job_id="b",
        batch_job_url="mock://batch-jobs/single",
        batch_job_status=BatchJobStatus.NOT_STARTED,
    )
    poller = _poller()
    await poller._poll_once()
    await poller._poll_parts_once()
    assert reload_job(engine, single.id).status == JobStatus.SUCCEEDED
    assert reload_job(engine, job.id).status == JobStatus.SUCCEEDED
    assert (
        reload_job(engine, single.id).dialogue_entries[0]["speaker"] == "Speaker 0"
    )  # the single-file transcript is as ever


async def test_the_webhook_of_a_job_in_parts_carries_the_merged_transcript_and_is_sent_once(engine, speech, mocker):
    from uuid import uuid4

    job = await _prepared(engine, callback_url="https://caller.example/hook", caller_id=uuid4())
    sent = []

    async def dispatch(url, secret, payload):
        sent.append((url, secret, payload))
        return True

    mocker.patch.object(polling_service, "dispatch", new=dispatch)
    one = BatchPollingService(webhook_secret_resolver=lambda _caller: "s3cret")
    two = BatchPollingService(webhook_secret_resolver=lambda _caller: "s3cret")

    await asyncio.gather(one._assemble(job.id), two._assemble(job.id))

    assert len(sent) == 1
    url, secret, payload = sent[0]
    assert (url, secret, payload["status"], payload["job_id"]) == (
        "https://caller.example/hook",
        "s3cret",
        "succeeded",
        str(job.id),
    )
    assert len(payload["dialogue_entries"]) == 6 and payload["dialogue_entries"][2]["text"].startswith("Part 2 ")


async def test_a_failed_job_in_parts_sends_one_failure_webhook(engine, speech, mocker):
    from uuid import uuid4

    await _prepared(engine, callback_url="https://caller.example/hook", caller_id=uuid4())
    speech.status = {2: "Failed"}
    sent = []

    async def dispatch(url, secret, payload):
        sent.append(payload)
        return True

    mocker.patch.object(polling_service, "dispatch", new=dispatch)
    poller = BatchPollingService(webhook_secret_resolver=lambda _caller: "s3cret")
    await poller._poll_parts_once()
    await poller._poll_parts_once()
    assert [(p["status"], p["error_message"]) for p in sent] == [
        ("failed", "Part 2 of 3 of the recording could not be transcribed. Please upload the recording again.")
    ]


async def test_two_parts_failing_in_the_same_poll_fail_the_job_once(engine, speech, tel):
    job = await _prepared(engine)
    speech.status = {2: "Failed", 3: "Failed"}
    poller = _poller()

    await poller._poll_parts_once()

    assert reload_job(engine, job.id).status == JobStatus.FAILED
    assert len(tel.events("job.finished")) == 1 and poller._dispatch_failure.await_count == 1
    assert len(speech.deleted) == 3
