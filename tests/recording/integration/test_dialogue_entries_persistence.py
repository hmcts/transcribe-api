"""Integration test: DIAAT-232's `alternatives` field round-trips through a
real Postgres JSONB column, not just through in-memory model_dump/reconstruct.

Requires a reachable Postgres (see docker-compose.yml's `postgres` service,
or point DATABASE_CONNECTION_STRING at any scratch database) — skipped
automatically otherwise, matching this suite's existing convention of unit
tests covering the JSONB round-trip in isolation for CI, with this file as
the higher-fidelity check run manually against a real database.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy
from sqlmodel import Session, SQLModel, create_engine

from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.domain.interface_recording import get_job_by_id, save_job_results
from transcribe_api.domain.models_recording import (
    BatchJobStatus,
    Caller,
    DialogueEntry,
    NBestCandidate,
    PhraseAlternatives,
    SpeechBatchJob,
    WordInfo,
)

# Bounded psycopg2 connect timeout so an unreachable Postgres fails fast
# instead of stalling test *collection* on the module-level skip check below,
# or hanging the run in the session fixture, for the full OS-default TCP
# timeout (conftest.py defaults DATABASE_CONNECTION_STRING to localhost).
_CONNECT_ARGS = {"connect_timeout": 2}


def _db_available(url: str) -> bool:
    engine = None
    try:
        engine = create_engine(url, connect_args=_CONNECT_ARGS)
        with engine.connect():
            return True
    except Exception:
        # create_engine itself can raise on a malformed connection string
        # (leaving `engine` unbound), so the dispose() below must guard for
        # that rather than assume the engine was created.
        return False
    finally:
        if engine is not None:
            engine.dispose()


_DB_URL = get_settings().DATABASE_CONNECTION_STRING
_ENVIRONMENT = get_settings().ENVIRONMENT
# The session fixture TRUNCATEs tables on teardown, so this test must never
# run against anything but a throwaway/scratch database. Gate it on
# ENVIRONMENT in {test, local} *as well as* DB reachability, so pointing
# DATABASE_CONNECTION_STRING at a real (staging/prod) database can't wipe it
# even by accident — a non-test environment simply skips before any truncate.
_IS_SCRATCH_ENV = _ENVIRONMENT in ("test", "local")
# `and` short-circuits, so _db_available() (a bounded ~2s connect probe) is
# only ever called in a scratch environment — a non-scratch environment
# decides "skip" without opening any DB connection at collection time.
_SHOULD_RUN = _IS_SCRATCH_ENV and _db_available(_DB_URL)
pytestmark = pytest.mark.skipif(
    not _SHOULD_RUN,
    reason=(
        "Live-DB round-trip test only runs against a scratch database "
        "(ENVIRONMENT in {test, local}) with a reachable Postgres"
    ),
)


@pytest.fixture
def session():
    engine = create_engine(_DB_URL, connect_args=_CONNECT_ARGS)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    # Leave no trace between runs against a shared scratch database.
    # TRUNCATE ... CASCADE (rather than ordered DELETEs) so any table with an
    # FK onto these — e.g. correction_dataset_entry references both
    # transcription_job and caller (see models.py) — is cleared too, instead
    # of the cleanup failing with a foreign-key violation. CASCADE also keeps
    # this robust as future tables add references to caller/transcription_job.
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("TRUNCATE TABLE transcription_job, caller CASCADE"))
    engine.dispose()


def _make_caller(session: Session) -> Caller:
    caller = Caller(
        name=f"itest-{uuid.uuid4()}",
        hashed_key="hashed",
        webhook_secret="secret",
    )
    session.add(caller)
    session.commit()
    session.refresh(caller)
    return caller


def test_full_nbest_array_survives_a_real_jsonb_round_trip(session):
    caller = _make_caller(session)
    job = SpeechBatchJob(
        caller_id=caller.id,
        audio_url="https://example.com/audio.wav",
        dialogue_entries=[],
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    job_id = job.id

    entries = [
        DialogueEntry(
            speaker="Speaker 0",
            text="Hello world.",
            start_time=0.76,
            end_time=2.08,
            confidence=0.5643338,
            words=[
                WordInfo(text="hello", start_time=0.76, end_time=1.52, confidence=0.95),
                WordInfo(text="world", start_time=1.52, end_time=2.08, confidence=0.6),
            ],
            alternatives=[
                PhraseAlternatives(
                    start_word_index=0,
                    end_word_index=1,
                    candidates=[
                        NBestCandidate(
                            text="Hello world.", confidence=0.5643338, lexical="hello world"
                        ),
                        NBestCandidate(
                            text="helloworld", confidence=0.1769063, lexical="helloworld"
                        ),
                        NBestCandidate(
                            text="hello worlds", confidence=0.49964225, lexical="hello worlds"
                        ),
                    ],
                )
            ],
        )
    ]

    save_job_results(session, job_id, entries, BatchJobStatus.SUCCEEDED)

    # Fresh session-less fetch: a brand new read from Postgres, not the ORM
    # instance already held in memory, so this genuinely exercises the JSONB
    # column rather than an identity-mapped Python object.
    session.expunge_all()
    reloaded = get_job_by_id(session, job_id)

    assert reloaded is not None
    raw_entry = reloaded.dialogue_entries[0]
    assert "alternatives" in raw_entry
    rebuilt = DialogueEntry(**raw_entry)

    assert rebuilt.alternatives is not None
    group = rebuilt.alternatives[0]
    assert group.start_word_index == 0
    assert group.end_word_index == 1
    assert [c.text for c in group.candidates] == [
        "Hello world.",
        "helloworld",
        "hello worlds",
    ]
    assert group.candidates[2].confidence == pytest.approx(0.49964225)
    # The already-existing top-choice fields are untouched by this change.
    assert rebuilt.text == "Hello world."
    assert rebuilt.confidence == pytest.approx(0.5643338)
    assert [w.text for w in rebuilt.words] == ["hello", "world"]
