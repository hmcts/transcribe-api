"""DIAAT-428: a hearing and its days (domain/hearings.py), on SQLite.

The days of a hearing are in order of their hearing date, then of upload, then of id; a job from before
hearings (no hearing_id: written by the previous release during the rollout) is a one-day hearing whose id
is the job's; the hearing's status follows its days' (processing while any is, completed once all are,
failed if all failed, partial otherwise); the list is one row per hearing, newest upload first, and
carries no transcript.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session

from tests.unit.recording.unit.async_upload_helpers import USER_ID, add_job, new_engine
from transcribe_api.domain import hearings
from transcribe_api.domain.models_recording import Hearing, JobStatus, SpeechBatchJob

IAC = UUID("00000000-0000-4000-8000-00000000000a")
OTHER_JURISDICTION = UUID("00000000-0000-4000-8000-00000000000b")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = new_engine(tmp_path / "hearings.db")
    yield engine
    engine.dispose()


def _hearing(
    engine, *, hearing_id: UUID | None = None, user_id: UUID = USER_ID, jurisdiction_id: UUID = IAC, **metadata
) -> UUID:
    hearing = Hearing(id=hearing_id or uuid4(), user_id=user_id, jurisdiction_id=jurisdiction_id, metadata_=metadata)
    with Session(engine) as session:
        session.add(hearing)
        session.commit()
        return hearing.id


def _day(
    engine, hearing_id: UUID | None, *, day: date | None = None, created: datetime = T0, **fields
) -> SpeechBatchJob:
    return add_job(
        engine,
        hearing_id=hearing_id,
        hearing_date=day,
        created_datetime=created,
        source_blob_path=None,
        jurisdiction_id=fields.pop("jurisdiction_id", IAC),
        **fields,
    )


def test_a_job_without_a_hearing_is_a_one_day_hearing_with_its_own_id():
    job = SpeechBatchJob(audio_url="https://x/a.wav", created_datetime=datetime(2026, 7, 1, 23, 30, tzinfo=UTC))
    assert hearings.hearing_of(job) == job.id
    assert hearings.day_date(job) == date(2026, 7, 2)  # the upload's date in London (BST)

    job.hearing_id, job.hearing_date = uuid4(), date(2026, 6, 30)
    assert hearings.hearing_of(job) == job.hearing_id
    assert hearings.day_date(job) == date(2026, 6, 30)


def test_london_date_follows_the_clock_change():
    assert hearings.london_date(datetime(2026, 1, 1, 23, 30, tzinfo=UTC)) == date(2026, 1, 1)
    assert hearings.london_date(datetime(2026, 10, 24, 23, 0, tzinfo=UTC)) == date(2026, 10, 25)
    assert hearings.london_date(datetime(2026, 10, 25, 23, 0, tzinfo=UTC)) == date(2026, 10, 25)


def test_days_are_ordered_by_hearing_date_then_upload_then_id(engine):
    hearing_id = _hearing(engine)
    third = _day(engine, hearing_id, day=date(2026, 11, 3), created=T0)
    second_b = _day(engine, hearing_id, day=date(2026, 11, 2), created=T0 + timedelta(hours=2))
    first = _day(engine, hearing_id, day=date(2026, 11, 1), created=T0 + timedelta(hours=5))
    second_a = _day(engine, hearing_id, day=date(2026, 11, 2), created=T0 + timedelta(hours=1))
    _day(engine, _hearing(engine), day=date(2026, 11, 1))  # another hearing's

    with Session(engine) as session:
        days = hearings.days_of(session, hearing_id)

    assert [d.id for d in days] == [first.id, second_a.id, second_b.id, third.id]


def test_same_date_and_time_falls_back_to_the_id(engine):
    hearing_id = _hearing(engine)
    ids = sorted([uuid4(), uuid4()], key=str)
    for job_id in reversed(ids):
        _day(engine, hearing_id, day=date(2026, 11, 2), id=job_id)
    with Session(engine) as session:
        assert [d.id for d in hearings.days_of(session, hearing_id)] == ids


def test_the_days_of_a_job_from_before_hearings_are_that_job(engine):
    legacy = _day(engine, None)
    with Session(engine) as session:
        assert [d.id for d in hearings.days_of(session, legacy.id)] == [legacy.id]
        found = hearings.find_hearing(session, legacy.id)
    assert found is not None and not found.stored
    assert (found.id, found.user_id, found.jurisdiction_id) == (legacy.id, USER_ID, IAC)


def test_find_hearing_reads_the_row_and_says_nothing_of_an_unknown_id(engine):
    hearing_id = _hearing(engine, case_reference="PA/1", tribunal="Taylor House")
    with Session(engine) as session:
        found = hearings.find_hearing(session, hearing_id)
        assert hearings.find_hearing(session, uuid4()) is None
    assert found.stored and found.metadata == {"case_reference": "PA/1", "tribunal": "Taylor House"}


def test_a_job_that_belongs_to_a_hearing_is_not_a_hearing_of_its_own(engine):
    day = _day(engine, _hearing(engine))
    with Session(engine) as session:
        assert hearings.find_hearing(session, day.id) is None


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([JobStatus.SUCCEEDED, JobStatus.SUCCEEDED], "completed"),
        ([JobStatus.SUCCEEDED, JobStatus.RUNNING], "processing"),
        ([JobStatus.FAILED, JobStatus.PENDING], "processing"),
        ([JobStatus.SUBMITTED], "processing"),
        ([JobStatus.FAILED, JobStatus.FAILED], "failed"),
        ([JobStatus.SUCCEEDED, JobStatus.FAILED], "partial"),
        ([JobStatus.SUCCEEDED], "completed"),
    ],
)
def test_hearing_status_follows_its_days(statuses, expected):
    days = [SpeechBatchJob(audio_url="https://x/a.wav", status=s) for s in statuses]
    assert hearings.hearing_status(days) == expected


def test_day_state_groups_the_job_statuses():
    states = {s: hearings.day_state(SpeechBatchJob(audio_url="x", status=s)) for s in JobStatus}
    assert states == {
        JobStatus.PENDING: "processing",
        JobStatus.SUBMITTED: "processing",
        JobStatus.RUNNING: "processing",
        JobStatus.SUCCEEDED: "complete",
        JobStatus.FAILED: "failed",
    }


def test_the_list_is_one_entry_per_hearing_newest_upload_first_with_its_days_in_order(engine):
    older = _hearing(engine, case_reference="OLD")
    _day(engine, older, day=date(2026, 10, 1), created=T0 - timedelta(days=30))
    newer = _hearing(engine, case_reference="NEW")
    newer_day_2 = _day(engine, newer, day=date(2026, 11, 3), created=T0 - timedelta(days=2))
    newer_day_1 = _day(engine, newer, day=date(2026, 11, 2), created=T0 - timedelta(days=3))
    legacy = _day(engine, None, created=T0 - timedelta(days=10))
    appended_to_older = _day(engine, older, day=date(2026, 10, 2), created=T0)  # the latest upload of all
    _day(engine, _hearing(engine, user_id=uuid4()), user_id=uuid4())  # someone else's
    _day(engine, _hearing(engine, jurisdiction_id=OTHER_JURISDICTION), jurisdiction_id=OTHER_JURISDICTION)

    with Session(engine) as session:
        page, total = hearings.list_hearings_paginated(session, USER_ID, IAC, limit=10, offset=0)

    assert total == 3
    assert [entry.hearing.id for entry in page] == [older, newer, legacy.id]
    assert [d.id for d in page[0].days][-1] == appended_to_older.id
    assert [d.id for d in page[1].days] == [newer_day_1.id, newer_day_2.id]
    assert page[1].hearing.metadata == {"case_reference": "NEW"}
    assert not page[2].hearing.stored


def test_the_list_pages(engine):
    ids = []
    for i in range(3):
        hearing_id = _hearing(engine)
        _day(engine, hearing_id, created=T0 - timedelta(days=i))
        ids.append(hearing_id)
    with Session(engine) as session:
        page, total = hearings.list_hearings_paginated(session, USER_ID, IAC, limit=2, offset=1)
    assert total == 3
    assert [entry.hearing.id for entry in page] == ids[1:]


def test_the_list_does_not_load_transcripts(engine):
    hearing_id = _hearing(engine)
    _day(
        engine,
        hearing_id,
        status=JobStatus.SUCCEEDED,
        dialogue_entries=[{"speaker": "A", "text": "secret", "start_time": 0, "end_time": 1}],
    )
    with Session(engine) as session:
        (entry,), _ = hearings.list_hearings_paginated(session, USER_ID, IAC, limit=10, offset=0)
        (day,) = entry.days
        assert "dialogue_entries" not in day.__dict__


def test_the_hearing_goes_once_it_has_no_day_left(engine):
    hearing_id = _hearing(engine)
    day = _day(engine, hearing_id)
    with Session(engine) as session:
        assert hearings.delete_hearing_if_empty(session, hearing_id) is False  # it still has a day
        session.delete(session.get(SpeechBatchJob, day.id))
        session.commit()
        assert hearings.delete_hearing_if_empty(session, hearing_id) is True
        assert session.get(Hearing, hearing_id) is None
