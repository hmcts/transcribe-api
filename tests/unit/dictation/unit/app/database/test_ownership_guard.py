"""SEC-1: saves never move an existing record to a different owner.

Runs the real save functions against an in-memory SQLite database (JSONB rendered
as JSON), so the guard is tested with real SQL, not mocks.
"""

from uuid import uuid4

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from transcribe_api.domain import interface_dictation as db
from transcribe_api.domain.exceptions import OwnershipConflictError
from transcribe_api.domain.models_dictation import MinuteVersion, Transcription, TranscriptionJob, User


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):
    return "JSON"


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [User.__table__, Transcription.__table__, TranscriptionJob.__table__, MinuteVersion.__table__]
    SQLModel.metadata.create_all(engine, tables=tables)
    monkeypatch.setattr(db, "engine", engine)
    return engine


def _user(engine) -> User:
    with Session(engine) as session:
        user = User(email=f"{uuid4()}@example.com", azure_user_id=str(uuid4()))
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _title(engine, transcription_id):
    with Session(engine) as session:
        return session.get(Transcription, transcription_id).title


def _owner(engine, transcription_id):
    with Session(engine) as session:
        return session.get(Transcription, transcription_id).user_id


def test_save_cannot_take_over_another_users_transcription(engine):
    victim, attacker = _user(engine), _user(engine)
    original = db.save_transcription(Transcription(title="Victim's hearing"), victim.id)

    with pytest.raises(OwnershipConflictError):
        db.save_transcription(Transcription(id=original.id, title="overwritten"), attacker.id)

    with Session(engine) as session:
        row = session.get(Transcription, original.id)
        assert row.user_id == victim.id
        assert row.title == "Victim's hearing"


def test_owner_can_still_update_and_new_ids_still_insert(engine):
    user = _user(engine)
    saved = db.save_transcription(Transcription(title="first"), user.id)
    db.save_transcription(Transcription(id=saved.id, title="second"), user.id)
    new_id = uuid4()
    db.save_transcription(Transcription(id=new_id), user.id)  # upload: ID from the file name
    assert _owner(engine, saved.id) == user.id
    assert _owner(engine, new_id) == user.id
    assert db.get_transcription_owner(new_id) == user.id
    assert db.get_transcription_owner(uuid4()) is None


def test_job_and_minute_version_cannot_move_to_another_transcription(engine):
    victim, attacker = _user(engine), _user(engine)
    victims = db.save_transcription(Transcription(), victim.id)
    attackers = db.save_transcription(Transcription(), attacker.id)
    job = db.save_transcription_job(TranscriptionJob(transcription_id=victims.id, dialogue_entries=[]))
    minutes = db.save_minute_version(MinuteVersion(transcription_id=victims.id, html_content="<p>v</p>"))

    with pytest.raises(OwnershipConflictError):
        db.save_transcription_job(TranscriptionJob(id=job.id, transcription_id=attackers.id, dialogue_entries=[]))
    with pytest.raises(OwnershipConflictError):
        db.save_minute_version(MinuteVersion(id=minutes.id, transcription_id=attackers.id, html_content="x"))

    with Session(engine) as session:
        assert session.get(TranscriptionJob, job.id).transcription_id == victims.id
        assert session.get(MinuteVersion, minutes.id).html_content == "<p>v</p>"


def test_server_generated_ids_skip_the_check_and_client_ids_keep_it(engine, monkeypatch):
    """check_owner=False (server-generated IDs only) avoids the locking query; the
    default keeps the SEC-1 guard for every other caller."""
    user = _user(engine)
    transcription = db.save_transcription(Transcription(), user.id)
    calls = []
    real_check = db._ensure_owner_unchanged
    monkeypatch.setattr(db, "_ensure_owner_unchanged", lambda *a, **kw: calls.append(a) or real_check(*a, **kw))

    db.save_minute_version(MinuteVersion(transcription_id=transcription.id, html_content="x"), check_owner=False)
    db.save_transcription_job(
        TranscriptionJob(transcription_id=transcription.id, dialogue_entries=[]), check_owner=False
    )
    assert calls == []

    db.save_minute_version(MinuteVersion(transcription_id=transcription.id, html_content="y"))
    db.save_transcription_job(TranscriptionJob(transcription_id=transcription.id, dialogue_entries=[]))
    assert len(calls) == 2


def test_an_upload_naming_your_own_transcription_keeps_its_fields(engine):
    """Retrying an upload of your own transcription must not reset it to defaults."""
    owner, other = _user(engine), _user(engine)
    saved = db.save_transcription(Transcription(title="Hearing 12 May"), owner.id)

    loaded = db.load_or_create_transcription(str(saved.id), owner.id)
    assert loaded.title == "Hearing 12 May"
    with Session(engine) as session:
        assert session.get(Transcription, saved.id).title == "Hearing 12 May"

    with pytest.raises(OwnershipConflictError):
        db.load_or_create_transcription(str(saved.id), other.id)

    new_id = uuid4()
    created = db.load_or_create_transcription(str(new_id), owner.id)
    assert created.id == new_id
    assert _title(engine, saved.id) == "Hearing 12 May"


def test_the_ownership_lock_does_not_block_child_inserts():
    """FOR NO KEY UPDATE, not FOR UPDATE: inserts of rows referencing the parent are
    not blocked by the guard (Postgres foreign keys take FOR KEY SHARE)."""
    from sqlalchemy.dialects import postgresql
    from sqlmodel import select

    statement = select(Transcription.user_id).where(Transcription.id == uuid4()).with_for_update(key_share=True)
    assert "FOR NO KEY UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
    import inspect

    assert "key_share=True" in inspect.getsource(db._ensure_owner_unchanged)
