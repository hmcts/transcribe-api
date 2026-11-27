"""tests/data/seed_local.py: what it refuses, the personas it creates, and a full seed of a migrated database."""

import pytest
from sqlmodel import Session, select

from tests.data import seed_local

CONTRACT_PERSONAS = {
    "judge-et": ([("ET", "Judge")], False),
    "judge-iac": ([("IAC", "Judge")], False),
    "multi": ([("ET", "Judge"), ("IAC", "JurisdictionAdministrator")], False),
    "many": (
        [("IAC", "Judge"), ("ET", "Judge"), ("ET", "LegalTextManager"), ("SSCS", "JurisdictionAdministrator")],
        False,
    ),
    "jadmin-iac": ([("IAC", "JurisdictionAdministrator")], False),
    "jadmin-et": ([("ET", "JurisdictionAdministrator")], False),
    "legal-iac": ([("IAC", "LegalTextManager")], False),
    "jsuper": ([(None, "JurisdictionSuperAdministrator")], False),
    "sysadmin": ([], True),
    "no-role": ([], False),
}
DEMO_SCENARIOS = ["baseline", "clean", "corrected", "failed", "needs_review", "running"]


def _clear_settings_caches():
    from transcribe_api.runtime import settings_dictation, settings_recording

    settings_dictation.get_settings.cache_clear()
    settings_recording.get_settings.cache_clear()


@pytest.fixture
def local_audio(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", seed_local.LOCAL_API_URL)
    monkeypatch.setenv("LOCAL_AUDIO_STORAGE_DIR", str(tmp_path))
    _clear_settings_caches()
    yield tmp_path
    _clear_settings_caches()


def test_refuses_outside_local_mode_without_connecting(monkeypatch, capsys):
    monkeypatch.setattr(seed_local, "create_engine", lambda *a, **k: pytest.fail("must not connect"))
    monkeypatch.setenv("ENVIRONMENT", "production")

    assert seed_local.main() == 1
    assert "ENVIRONMENT is not local" in capsys.readouterr().out


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:secret@prod-db.postgres.database.azure.com:5432/transcribe",
        "postgresql://user:secret@10.0.0.5:5432/transcribe",
        "postgresql://user:secret@localhost/db?host=shared-db",
    ],
)
def test_refuses_a_database_that_is_not_the_developers_own_without_connecting(monkeypatch, capsys, url):
    monkeypatch.setattr(seed_local, "create_engine", lambda *a, **k: pytest.fail("must not connect"))
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("DATABASE_CONNECTION_STRING", url)
    _clear_settings_caches()

    assert seed_local.main() == 1
    out = capsys.readouterr().out
    assert "Refusing to run against DATABASE_CONNECTION_STRING" in out
    assert "secret" not in out


def test_the_personas_are_the_contracts():
    from transcribe_api.domain.auth.dev_personas import PERSONAS

    assert {name: (list(p.assignments), p.sysadmin) for name, p in PERSONAS.items()} == CONTRACT_PERSONAS
    assert {p.email for p in PERSONAS.values()} == {f"{name}@localhost.test" for name in CONTRACT_PERSONAS}
    assert len({p.azure_user_id for p in PERSONAS.values()}) == len(PERSONAS)


@pytest.mark.real_jurisdictions
def test_creates_the_personas_jurisdictions_users_and_assignments_and_is_idempotent(jurisdiction_db):
    from transcribe_api.domain.models_dictation import Jurisdiction, RoleAssignment, User

    with Session(jurisdiction_db) as session:
        first = seed_local.seed_personas(session)
        session.commit()
    with Session(jurisdiction_db) as session:
        second = seed_local.seed_personas(session)
        session.commit()
        jurisdictions = {j.code: j for j in session.exec(select(Jurisdiction)).all()}
        users = session.exec(select(User)).all()
        assignments = session.exec(select(RoleAssignment)).all()

    assert sorted(first["jurisdictions"]) == ["ET", "IAC", "SSCS"]
    assert len(first["users"]) == 10 and len(first["assignments"]) == 12
    assert second == {"jurisdictions": [], "users": [], "assignments": []}
    assert jurisdictions["ET"].name == "Employment Tribunal"
    assert jurisdictions["IAC"].name == "Immigration and Asylum Chamber" and jurisdictions["IAC"].enabled
    assert len(users) == 10 and len(assignments) == 12
    assert all(u.role is None and u.is_active for u in users)


@pytest.mark.real_jurisdictions
def test_leaves_an_existing_iac_and_existing_assignments_as_they_are(jurisdiction_db):
    from tests.unit.jurisdiction_fixtures import add_iac_as_migrated
    from transcribe_api.domain.models_dictation import Jurisdiction

    iac = add_iac_as_migrated(jurisdiction_db)
    with Session(jurisdiction_db) as session:
        seed_local.seed_personas(session)
        session.commit()
        row = session.exec(select(Jurisdiction).where(Jurisdiction.code == "IAC")).one()
    assert row.id == iac.id and row.settings == iac.settings


def test_demo_jobs_need_local_audio_storage(monkeypatch):
    assert seed_local.demo_audio_unavailable() == "AUDIO_STORAGE_BACKEND is not local"

    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    _clear_settings_caches()
    assert seed_local.demo_audio_unavailable() == "LOCAL_AUDIO_BASE_URL is not set"

    monkeypatch.setenv("LOCAL_AUDIO_BASE_URL", seed_local.LOCAL_API_URL)
    _clear_settings_caches()
    assert seed_local.demo_audio_unavailable() is None


@pytest.mark.real_jurisdictions
def test_seeds_a_freshly_migrated_database_and_replaces_its_demo_jobs_when_run_again(fresh_postgres, local_audio):
    """Run against every migration: a migration that breaks the seed fails here, before it is pushed."""
    from transcribe_api.domain.auth.dev_personas import PERSONAS
    from transcribe_api.domain.models_dictation import Jurisdiction, RoleAssignment, User
    from transcribe_api.domain.models_recording import SpeechBatchJob
    from transcribe_api.stt import local_storage

    first_personas, first_jobs = seed_local.seed(fresh_postgres, with_demo_jobs=True)
    second_personas, second_jobs = seed_local.seed(fresh_postgres, with_demo_jobs=True)

    with Session(fresh_postgres) as session:
        owner = session.exec(select(User).where(User.azure_user_id == PERSONAS["judge-iac"].azure_user_id)).one()
        iac = session.exec(select(Jurisdiction).where(Jurisdiction.code == "IAC")).one()
        users = session.exec(select(User)).all()
        assignments = session.exec(select(RoleAssignment)).all()
        jobs = session.exec(select(SpeechBatchJob)).all()

    assert len(first_personas["users"]) == 10 and len(first_personas["assignments"]) == 12
    assert second_personas == {"jurisdictions": [], "users": [], "assignments": []}
    assert len(users) == 10 and len(assignments) == 12

    assert {job.id for job in jobs} == {job.id for job in second_jobs}
    assert sorted(job.metadata_["seed_scenario"] for job in jobs) == DEMO_SCENARIOS
    assert all(job.user_id == owner.id and job.jurisdiction_id == iac.id for job in jobs)
    assert all(local_storage.size(job.audio_blob_path) > 0 for job in second_jobs)
    for replaced in first_jobs:
        with pytest.raises(FileNotFoundError):
            local_storage.size(replaced.audio_blob_path)


@pytest.mark.real_jurisdictions
def test_seeds_only_the_personas_without_demo_jobs(fresh_postgres):
    from transcribe_api.domain.models_recording import SpeechBatchJob

    created, jobs = seed_local.seed(fresh_postgres, with_demo_jobs=False)

    with Session(fresh_postgres) as session:
        assert session.exec(select(SpeechBatchJob)).all() == []
    assert len(created["users"]) == 10 and jobs == []
