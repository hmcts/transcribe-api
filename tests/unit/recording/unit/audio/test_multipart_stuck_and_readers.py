"""DIAAT-408: a recording in parts in the job gauges and the stuck rule, and what else reads such a job.

The gauges count a job once, whatever its parts. A job in parts is reported as stuck against its longest
part (the parts run at the same time), not against the fixed single-file threshold; a single-file job is
judged exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry
from sqlmodel import Session

from tests.unit.jurisdiction_fixtures import IAC_JURISDICTION_ID
from tests.unit.recording.unit.async_upload_helpers import USER_ID, add_job, new_engine
from transcribe_api.api.app import create_app
from transcribe_api.domain import interface_recording as jobs
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.dependencies_recording import get_current_user
from transcribe_api.domain.models_recording import JobStatus, User
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.telemetry import JOURNEY_JOB, configure_telemetry
from transcribe_api.stt import job_telemetry

HOUR = 3600.0
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = new_engine(tmp_path / "jobs.db")
    yield engine
    engine.dispose()


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


def _running(engine, *, hours_ago: float, part_hours: list[float] | None = None):
    """A RUNNING job that started processing ``hours_ago`` hours before NOW, in parts of the given lengths."""
    started = NOW - timedelta(hours=hours_ago)
    job = add_job(
        engine,
        status=JobStatus.RUNNING,
        created_datetime=started - timedelta(minutes=2),
        processing_started_datetime=started,
        batch_job_id=None if part_hours else "single",
        source_blob_path="incoming/x.wav" if part_hours else None,
    )
    if part_hours:
        offset = 0.0
        plans = []
        for i, length in enumerate(part_hours, 1):
            plans.append(
                jobs.PartPlan(
                    i, len(part_hours), offset, length * HOUR, "start" if i == 1 else "silence", f"uploads/p{i}"
                )
            )
            offset += length * HOUR
        with Session(engine) as session:
            jobs.create_job_parts(session, job.id, plans)
    return job


def _counts(engine):
    with Session(engine) as session:
        return jobs.count_job_states(
            session,
            waiting_cutoff=NOW - timedelta(seconds=get_settings().JOB_STUCK_WAITING_SECONDS),
            running_cutoff=NOW - timedelta(seconds=get_settings().JOB_STUCK_RUNNING_SECONDS),
            now=NOW,
        )


def test_the_default_factor_is_one_and_a_half_and_must_be_positive():
    from pydantic import ValidationError

    from transcribe_api.runtime.settings_recording import Settings

    assert Settings().JOB_STUCK_PART_FACTOR == 1.5
    with pytest.raises(ValidationError):
        Settings(JOB_STUCK_PART_FACTOR=0)


def test_a_job_in_parts_is_stuck_after_one_and_a_half_times_its_longest_part_not_after_six_hours(engine):
    # Parts of 2 h 40 m (2.667 h): stuck after 4 h, long before the single-file 6 h.
    four_hours_thirty = _running(engine, hours_ago=4.5, part_hours=[2.667, 2.667, 2.666])
    three_hours = _running(engine, hours_ago=3.0, part_hours=[2.667, 2.667, 2.666])

    with Session(engine) as session:
        stuck = {j.id for j in jobs.multipart_stuck_running(session, NOW)}
    assert stuck == {four_hours_thirty.id} and three_hours.id not in stuck
    assert _counts(engine).stuck_running == 1


def test_the_threshold_follows_the_longest_part_of_the_job(engine):
    short_parts = _running(engine, hours_ago=2.0, part_hours=[1.0, 1.0])  # 1.5 x 1 h = 1.5 h: stuck at 2 h
    long_parts = _running(engine, hours_ago=2.0, part_hours=[3.5, 1.0])  # 1.5 x 3.5 h = 5.25 h: not stuck at 2 h
    with Session(engine) as session:
        assert {j.id for j in jobs.multipart_stuck_running(session, NOW)} == {short_parts.id}
    assert long_parts.id


def test_a_single_file_job_is_judged_by_the_fixed_threshold_exactly_as_before(engine):
    five_hours = _running(engine, hours_ago=5.0)  # under 6 h
    seven_hours = _running(engine, hours_ago=7.0)  # over 6 h
    in_parts_five = _running(
        engine, hours_ago=5.0, part_hours=[1.0, 1.0]
    )  # in parts, so by its parts: stuck, but not counted twice

    counts = _counts(engine)
    assert counts.running == 3
    assert counts.stuck_running == 2  # the 7 h single-file job, and the job in parts (once)
    with Session(engine) as session:
        claimed = {
            j.id
            for j in jobs.claim_stuck_jobs(
                session, waiting_cutoff=NOW - timedelta(minutes=30), running_cutoff=NOW - timedelta(hours=6), now=NOW
            )
        }
    assert claimed == {seven_hours.id, in_parts_five.id} and five_hours.id not in claimed


def test_a_job_in_parts_with_long_parts_is_not_stuck_by_the_single_file_threshold(engine):
    # One 5 h part: 1.5 x 5 h = 7.5 h. Running for 7 h it is past the single-file 6 h but not stuck.
    job = _running(engine, hours_ago=7.0, part_hours=[5.0])
    assert _counts(engine).stuck_running == 0
    with Session(engine) as session:
        claimed = jobs.claim_stuck_jobs(
            session, waiting_cutoff=NOW - timedelta(minutes=30), running_cutoff=NOW - timedelta(hours=6), now=NOW
        )
    assert claimed == [] and job.id


def test_a_job_in_parts_counts_once_in_the_gauges_not_once_per_part(engine):
    _running(engine, hours_ago=0.1, part_hours=[1.0, 1.0, 1.0])
    add_job(engine, status=JobStatus.SUBMITTED, source_blob_path="incoming/y.wav")
    counts = _counts(engine)
    assert counts.running == 1 and counts.waiting == 1 and counts.stuck_running == 0


def test_a_stuck_job_in_parts_is_reported_once_on_its_journey_without_an_azure_reference(engine, tel):
    job = _running(engine, hours_ago=3.0, part_hours=[1.0, 1.0])
    with Session(engine) as session:
        assert job_telemetry.report_stuck_jobs(session, NOW) == 1
    with Session(engine) as session:
        assert job_telemetry.report_stuck_jobs(session, NOW + timedelta(hours=1)) == 0  # not again
    (event,) = tel.events("job.stuck")
    assert event[c.JOURNEY_ID] == str(job.id) and event[c.JOURNEY_TYPE] == JOURNEY_JOB
    assert event["job.state"] == "running" and c.DEPENDENCY_REF not in event


# What else reads the job ---------------------------------------------------------------------------


@pytest.fixture
def client(engine):
    app = create_app()
    user = AuthenticatedUser(
        db_user=User(id=USER_ID, azure_user_id="oid", email="judge@example.test", role="Judge"),
        ctx=Context("IAC", "Judge"),
    )
    app.dependency_overrides[get_current_user] = lambda: user

    def session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = session
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.SUBMITTED, JobStatus.RUNNING, JobStatus.FAILED])
def test_the_job_and_the_list_show_a_job_in_parts_without_any_azure_job_of_its_own(client, engine, status):
    job = add_job(
        engine,
        status=status,
        source_blob_path=f"incoming/{uuid4()}.wav",
        jurisdiction_id=IAC_JURISDICTION_ID,
        error_message="Part 2 of 3 failed" if status == JobStatus.FAILED else None,
    )
    with Session(engine) as session:
        jobs.create_job_parts(session, job.id, [jobs.PartPlan(1, 1, 0.0, 10.0, "start", "uploads/p1")])

    one = client.get(f"/api/v1/jobs/{job.id}")
    assert one.status_code == 200
    assert one.json()["status"] == status.value
    assert one.json()["error_message"] == job.error_message
    listed = client.get("/api/v1/jobs").json()
    assert [j["job_id"] for j in listed["jobs"]] == [str(job.id)] and listed["total"] == 1
