"""Integration test (DIAAT-286): audit rows and provenance in a real Postgres.

Skipped unless a scratch Postgres is reachable (ENVIRONMENT in {test, local}), like
test_dialogue_entries_persistence.py. Run against a database migrated with
``alembic upgrade head`` to check that migrations 016/017 and the models agree.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy
from hmcts_otel import request_id_context
from sqlmodel import Session, SQLModel, create_engine, select

from transcribe_api.domain import audit_recording as audit
from transcribe_api.domain.audit_recording import AuditRecord, denied, write_audit
from transcribe_api.domain.audit_recording import _insert as real_insert
from transcribe_api.runtime.db_recording import get_engine
from transcribe_api.domain.interface_recording import get_job_by_id, save_job_results
from transcribe_api.domain import audit as audit_core
from transcribe_api.domain.models_dictation import AuditLog  # the shared audit_log
from transcribe_api.domain.models_recording import (
    BatchJobStatus,
    DialogueEntry,
    SpeechBatchJob,
)
from transcribe_api.domain.provenance_recording import TranscriptProvenance

from .test_dialogue_entries_persistence import _CONNECT_ARGS, _DB_URL, _SHOULD_RUN

pytestmark = pytest.mark.skipif(
    not _SHOULD_RUN,
    reason="Live-DB test only runs against a scratch database with a reachable Postgres",
)

ACTOR = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
REQUEST_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine(_DB_URL, connect_args=_CONNECT_ARGS)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(audit, "_insert", real_insert)  # the real insert, not the capture
    monkeypatch.setattr(audit_core, "get_engine", lambda: engine)  # the shared writer's engine
    get_engine.cache_clear()
    yield engine
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("TRUNCATE TABLE audit_log, transcription_job CASCADE"))
    engine.dispose()
    get_engine.cache_clear()


def test_audit_rows_are_inserted_and_queryable_by_job(engine):
    job_id, other = uuid.uuid4(), uuid.uuid4()
    with request_id_context(REQUEST_ID):
        write_audit(
            AuditRecord("transcript.view", "success", ACTOR, job_id, detail={"partial": True}),
            denied("transcript.view", ACTOR, other, "ownership_assertion_failed"),
        )

    with Session(engine) as session:
        rows = session.exec(select(AuditLog).where(AuditLog.object_id == str(job_id))).all()

    # The recording fields, in the shared table's columns.
    (row,) = rows
    assert (row.user_id, row.event_type, row.outcome) == (str(ACTOR), "transcript.view", "success")
    assert (row.request_id, row.detail) == (REQUEST_ID, {"partial": True})
    assert (row.held_roles, row.required_roles) == ([], [])
    with engine.connect() as conn:
        # No detail is SQL NULL, not a JSON null.
        nulls = conn.execute(sqlalchemy.text("SELECT count(*) FROM audit_log WHERE detail IS NULL")).scalar_one()
    assert nulls == 0  # both rows have a detail here
    write_audit(AuditRecord("job.delete", "success", ACTOR, job_id))
    with engine.connect() as conn:
        nulls = conn.execute(sqlalchemy.text("SELECT count(*) FROM audit_log WHERE detail IS NULL")).scalar_one()
    assert nulls == 1


def test_provenance_round_trips_with_the_transcript(engine):
    with Session(engine) as session:
        job = SpeechBatchJob(audio_url="https://example.com/a.wav", dialogue_entries=[])
        session.add(job)
        session.commit()
        job_id = job.id

        provenance = TranscriptProvenance.model_validate(
            {
                "app_release": "abc1234",
                "speech": {
                    "endpoint_host": "test.cognitiveservices.azure.com",
                    "region": "uksouth",
                    "api_version": "v3.2",
                    "locale": "en-GB",
                    "diarization": True,
                    "diarization_max_speakers": 35,
                    "model_ref": None,
                    "model_display_name": None,
                    "azure_job_id": "job-1",
                    "profanity_filter_mode": "None",
                    "punctuation_mode": "DictatedAndAutomatic",
                    "word_level_timestamps": True,
                },
                "post_processing": {
                    "spelling_dictionary_version": "0.1.6",
                    "speaker_steps_applied": ["speaker_grouping"],
                    "speaker_step_degraded": "speaker_normalisation",
                },
            }
        )
        entries = [DialogueEntry(speaker="1", text="x", start_time=0.0, end_time=1.0)]
        save_job_results(session, job_id, entries, BatchJobStatus.SUCCEEDED, provenance=provenance)
        session.expunge_all()
        reloaded = get_job_by_id(session, job_id)

    assert reloaded is not None and reloaded.provenance is not None
    stored = TranscriptProvenance.model_validate(reloaded.provenance)
    assert stored.speech.azure_job_id == "job-1"
    assert stored.post_processing.speaker_step_degraded == "speaker_normalisation"
    assert stored.generated_at is not None


def test_a_job_without_provenance_stores_sql_null(engine):
    with Session(engine) as session:
        session.add(SpeechBatchJob(audio_url="https://example.com/a.wav", dialogue_entries=[]))
        session.commit()
    with engine.connect() as conn:
        count = conn.execute(
            sqlalchemy.text("SELECT count(*) FROM transcription_job WHERE provenance IS NULL")
        ).scalar_one()
    assert count == 1
