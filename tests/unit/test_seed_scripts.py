"""The local seed scripts refuse non-local databases before connecting."""

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(filename: str):
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py").replace("-", "_"), _REPO_ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "filename", ["scripts-seed-local.py", "scripts-seed-demo-data.py", "scripts-seed-personas.py"]
)
def test_refuses_a_redirected_database_url_without_connecting(monkeypatch, filename):
    from transcribe_api.runtime.settings_recording import get_settings

    script = _load_script(filename)
    monkeypatch.setattr(script, "create_engine", lambda *a, **k: pytest.fail("must not connect"))
    if hasattr(script, "_ensure_seed_audio"):
        monkeypatch.setattr(script, "_ensure_seed_audio", lambda: None)
    monkeypatch.setenv("DATABASE_CONNECTION_STRING", "postgresql://u:p@localhost/db?host=shared-db")
    get_settings.cache_clear()
    try:
        assert script.main() == 1
    finally:
        get_settings.cache_clear()


# scripts-seed-personas.py (DIAAT-373) ---------------------------------------------------------

CONTRACT_PERSONAS = {
    "judge-et": ([("ET", "Judge")], False),
    "judge-iac": ([("IAC", "Judge")], False),
    "multi": ([("ET", "Judge"), ("IAC", "JurisdictionAdministrator")], False),
    "jadmin-iac": ([("IAC", "JurisdictionAdministrator")], False),
    "jadmin-et": ([("ET", "JurisdictionAdministrator")], False),
    "legal-iac": ([("IAC", "LegalTextManager")], False),
    "jsuper": ([(None, "JurisdictionSuperAdministrator")], False),
    "sysadmin": ([], True),
    "no-role": ([], False),
}


def test_the_personas_are_the_contracts():
    from transcribe_api.domain.auth.dev_personas import PERSONAS

    assert {name: (list(p.assignments), p.sysadmin) for name, p in PERSONAS.items()} == CONTRACT_PERSONAS
    assert {p.email for p in PERSONAS.values()} == {f"{name}@localhost.test" for name in CONTRACT_PERSONAS}
    assert len({p.azure_user_id for p in PERSONAS.values()}) == len(PERSONAS)


def test_the_personas_script_refuses_outside_local_mode_without_connecting(monkeypatch, capsys):
    script = _load_script("scripts-seed-personas.py")
    monkeypatch.setattr(script, "create_engine", lambda *a, **k: pytest.fail("must not connect"))
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert script.main() == 1
    assert "ENVIRONMENT is not local" in capsys.readouterr().out


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:secret@prod-db.postgres.database.azure.com:5432/transcribe",
        "postgresql://user:secret@10.0.0.5:5432/transcribe",
    ],
)
def test_the_personas_script_refuses_a_database_that_is_not_the_developers_own_without_connecting(
    monkeypatch, capsys, url
):
    """In local mode but pointed at a shared database: the second guard. It never connects, never seeds."""
    from transcribe_api.runtime.settings_dictation import get_settings

    script = _load_script("scripts-seed-personas.py")
    monkeypatch.setattr(script, "create_engine", lambda *a, **k: pytest.fail("must not connect"))
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("DATABASE_CONNECTION_STRING", url)
    get_settings.cache_clear()
    try:
        assert script.main() == 1
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "Refusing to run against DATABASE_CONNECTION_STRING" in out
    assert "secret" not in out  # the password is not echoed


@pytest.fixture
def personas_db(jurisdiction_db):
    return jurisdiction_db


@pytest.mark.real_jurisdictions
def test_the_personas_script_creates_the_jurisdictions_users_and_assignments_and_is_idempotent(personas_db):
    from sqlmodel import Session, select

    from transcribe_api.domain.models_dictation import Jurisdiction, RoleAssignment, User

    script = _load_script("scripts-seed-personas.py")
    with Session(personas_db) as session:
        first = script.seed(session)
        session.commit()
    with Session(personas_db) as session:
        second = script.seed(session)
        session.commit()
        jurisdictions = {j.code: j for j in session.exec(select(Jurisdiction)).all()}
        users = session.exec(select(User)).all()
        assignments = session.exec(select(RoleAssignment)).all()

    assert sorted(first["jurisdictions"]) == ["ET", "IAC"]
    assert len(first["users"]) == 9 and len(first["assignments"]) == 8
    assert second == {"jurisdictions": [], "users": [], "assignments": []}  # nothing the second time
    assert jurisdictions["ET"].name == "Employment Tribunal"
    assert jurisdictions["IAC"].name == "Immigration and Asylum Chamber" and jurisdictions["IAC"].enabled
    assert len(users) == 9 and len(assignments) == 8
    assert all(u.role is None and u.is_active for u in users)


@pytest.mark.real_jurisdictions
def test_the_personas_script_leaves_an_existing_iac_and_existing_assignments_as_they_are(personas_db):
    from sqlmodel import Session, select

    from tests.unit.jurisdiction_fixtures import add_iac_as_migrated
    from transcribe_api.domain.models_dictation import Jurisdiction

    iac = add_iac_as_migrated(personas_db)
    script = _load_script("scripts-seed-personas.py")
    with Session(personas_db) as session:
        script.seed(session)
        session.commit()
        row = session.exec(select(Jurisdiction).where(Jurisdiction.code == "IAC")).one()
    assert row.id == iac.id and row.settings == iac.settings
