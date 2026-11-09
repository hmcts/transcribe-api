"""Concurrent first logins for one user must both resolve to the same User (DIAAT-289).

AAT, 2026-10-28: two first-login requests for the same user both missed the
lookup and both inserted; the loser's UniqueViolation on ix_user_azure_user_id
surfaced as a 500 from GET /api/user/onboarding-status.

Since DIAAT-373 a user is created at sign-in (domain/auth/sessions.sign_in), not by a
request's resolver (which only loads the user a BenchNotes token names: tested here too).
Sign-in runs against a real database, so the unique index, the rollback and the re-select
are exercised rather than mocked. The SQLite tests always run; the Postgres test races two
threads for real and needs TEST_POSTGRES_URL (a throwaway Postgres), like
tests/unit/test_migrations.py.
"""

from __future__ import annotations

import importlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlmodel import Session, SQLModel, create_engine, select

from transcribe_api.domain.auth.sessions import sign_in
from transcribe_api.domain.models_dictation import Jurisdiction, RoleAssignment, User

RESOLVERS = [
    pytest.param("transcribe_api.domain.auth.dependencies_dictation", id="dictation"),
    pytest.param("transcribe_api.domain.auth.dependencies_recording", id="recording"),
]

AZURE_USER_ID = "3f2a9c1e-0000-4000-8000-000000000001"
EMAIL = "judge@example.test"


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):
    return "JSON"


# The user and, since DIAAT-322, the jurisdiction tables a first login writes to.
FIRST_LOGIN_TABLES = [User.__table__, Jurisdiction.__table__, RoleAssignment.__table__]


@pytest.fixture
def sqlite_engine(tmp_path):
    # A file rather than :memory: so the winning request gets its own connection.
    engine = create_engine(f"sqlite:///{tmp_path / 'users.db'}")
    SQLModel.metadata.create_all(engine, tables=FIRST_LOGIN_TABLES)
    yield engine
    engine.dispose()


def _commit_user(engine) -> User:
    with Session(engine) as session:
        user = User(email=EMAIL, azure_user_id=AZURE_USER_ID)
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def _users(engine) -> list[User]:
    with Session(engine) as session:
        return list(session.exec(select(User)).all())


def _losing_session(engine) -> type[Session]:
    """Session class for the request that loses the race.

    Its lookup runs against the empty table; the winner then commits the same
    user before this request's INSERT is flushed. Only a flush with the insert
    pending: the lookup's autoflush, with nothing pending, comes first and would
    commit the winner before the lookup, which would then simply find it.
    """

    class LosingSession(Session):
        winner: User | None = None
        rolled_back = False

        def flush(self, *args, **kwargs):
            if LosingSession.winner is None and self.new:
                LosingSession.winner = _commit_user(engine)
            super().flush(*args, **kwargs)

        def rollback(self):
            LosingSession.rolled_back = True
            super().rollback()

    return LosingSession


def _sign_in(session_class, engine, email=EMAIL):
    with session_class(engine) as session:
        return sign_in(session, azure_user_id=AZURE_USER_ID, email=email, name="Judge Real")


def test_a_first_login_creates_the_user_with_no_role(sqlite_engine):
    result = _sign_in(Session, sqlite_engine)

    [stored] = _users(sqlite_engine)
    assert result.id == stored.id
    assert (stored.email, stored.azure_user_id, stored.name) == (EMAIL, AZURE_USER_ID, "Judge Real")
    # Nothing of the old default role: a new user has no role (DIAAT-382), and is active.
    assert stored.role is None and stored.is_active is True
    assert stored.has_completed_onboarding is False


def test_the_request_that_loses_the_insert_gets_the_winners_row(sqlite_engine):
    losing_session = _losing_session(sqlite_engine)

    result = _sign_in(losing_session, sqlite_engine)

    # The race really happened: this request's insert failed and it recovered.
    assert losing_session.rolled_back
    assert result.id == losing_session.winner.id
    assert result.azure_user_id == AZURE_USER_ID
    assert len(_users(sqlite_engine)) == 1


def test_an_unrelated_integrity_error_is_not_mistaken_for_the_race(sqlite_engine):
    # An email of None fails the INSERT (NOT NULL) with no row to fall back on.
    with pytest.raises(IntegrityError):
        _sign_in(Session, sqlite_engine, email=None)

    assert _users(sqlite_engine) == []


# --- the resolver of a request: loads, never creates ----------------------------------------


def _principal(user_id, role="Judge", sysadmin=False):
    from datetime import UTC, datetime

    from transcribe_api.domain.auth.app_token import AppPrincipal, Context

    ctx = Context("IAC", role)
    return AppPrincipal(
        user_id=user_id,
        sysadmin=sysadmin,
        assignments=(ctx,),
        ctx=ctx,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("module_name", RESOLVERS)
def test_the_resolver_loads_the_user_the_token_names_and_derives_the_context_and_sysadmin(module_name, sqlite_engine):
    deps = importlib.import_module(module_name)
    user = _commit_user(sqlite_engine)

    with patch.object(deps, "get_engine", return_value=sqlite_engine):
        result = deps._resolve_user(_principal(user.id, "LegalTextManager", sysadmin=True))

    assert result.db_user.id == user.id
    assert result.ctx.role == "LegalTextManager" and result.sysadmin is True


@pytest.mark.parametrize("module_name", RESOLVERS)
def test_the_resolver_never_rewrites_user_role_from_the_token(module_name, sqlite_engine):
    """DIAAT-375: the Users tab's change of role had no lasting effect because every request put
    the Entra role back. User.role is not read or written by a request any more."""
    deps = importlib.import_module(module_name)
    user = _commit_user(sqlite_engine)
    with Session(sqlite_engine) as session:
        stored = session.get(User, user.id)
        stored.role = "LegalTextManager"
        session.add(stored)
        session.commit()

    with patch.object(deps, "get_engine", return_value=sqlite_engine):
        deps._resolve_user(_principal(user.id, "Judge", sysadmin=True))

    [after] = _users(sqlite_engine)
    assert after.role == "LegalTextManager"


@pytest.mark.parametrize("module_name", RESOLVERS)
def test_the_resolver_does_not_create_a_user_a_token_names_but_the_database_lacks(module_name, sqlite_engine):
    from uuid import uuid4

    from fastapi import HTTPException

    deps = importlib.import_module(module_name)
    with patch.object(deps, "get_engine", return_value=sqlite_engine), pytest.raises(HTTPException) as refused:
        deps._resolve_user(_principal(uuid4()))
    assert refused.value.status_code == 401
    assert _users(sqlite_engine) == []


@pytest.mark.parametrize("module_name", RESOLVERS)
def test_the_resolver_refuses_a_deactivated_user_and_does_not_reenable_them(module_name, sqlite_engine):
    from fastapi import HTTPException

    deps = importlib.import_module(module_name)
    user = _commit_user(sqlite_engine)
    with Session(sqlite_engine) as session:
        stored = session.get(User, user.id)
        stored.is_active = False
        session.add(stored)
        session.commit()

    with patch.object(deps, "get_engine", return_value=sqlite_engine), pytest.raises(HTTPException) as refused:
        deps._resolve_user(_principal(user.id))
    assert refused.value.status_code == 403 and refused.value.detail["code"] == "user_deactivated"
    assert [u.is_active for u in _users(sqlite_engine)] == [False]


def test_sign_in_refuses_a_deactivated_user_and_does_not_reenable_or_recreate_them(sqlite_engine):
    from fastapi import HTTPException

    user = _commit_user(sqlite_engine)
    with Session(sqlite_engine) as session:
        stored = session.get(User, user.id)
        stored.is_active = False
        session.add(stored)
        session.commit()

    with pytest.raises(HTTPException) as refused:
        _sign_in(Session, sqlite_engine)
    assert refused.value.status_code == 403
    [after] = _users(sqlite_engine)
    assert after.id == user.id and after.is_active is False


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
def test_concurrent_first_logins_on_postgres_resolve_to_one_user():
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    SQLModel.metadata.create_all(engine, tables=FIRST_LOGIN_TABLES)

    # Both requests must miss the lookup before either inserts. The insert is flushed
    # before the commit (the jurisdictions go in the same transaction, DIAAT-322), so
    # they meet at the flush of their insert: the second then waits on the first's
    # row. Not at the lookup's autoflush (nothing pending), which comes first: one
    # could then insert and commit before the other looked, and there is no race.
    both_looked_up = threading.Barrier(2, timeout=10)

    rollbacks = []

    class RacingSession(Session):
        def flush(self, *args, **kwargs):
            if self.new and not getattr(self, "_met", False):
                self._met = True
                both_looked_up.wait()
            super().flush(*args, **kwargs)

        def rollback(self):
            rollbacks.append(1)
            super().rollback()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_sign_in, RacingSession, engine) for _ in range(2)]
            first, second = (future.result() for future in futures)

        assert first.id == second.id
        assert len(_users(engine)) == 1
        # The race really happened: one insert failed and that request recovered.
        assert len(rollbacks) == 1
    finally:
        engine.dispose()
