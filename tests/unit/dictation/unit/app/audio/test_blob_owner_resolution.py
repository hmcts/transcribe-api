"""Blob owner resolution: the poller must find the user a dictation upload belongs to.

Uploads are written to user-uploads/{azure_user_id}/{file} (get_file_blob_path), and
legacy blobs to user-uploads/{email}/{file}. The poller used to treat every owner
segment as an email, so OID-keyed uploads never found a user and were never
transcribed. These tests run the resolver against a real (SQLite) session rather
than a mocked `.first()`, so the actual query is exercised.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from transcribe_api.domain.blob_ownership import (
    AmbiguousBlobOwnerError,
    resolve_blob_owner,
)
from transcribe_api.domain.models_dictation import User
from transcribe_api.stt.utils import get_file_blob_path
from transcribe_api.stt.work_poller import TranscriptionPollingService


@pytest.fixture
def db_engine():
    # Only the user table: the other dictation tables use Postgres JSONB.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    User.__table__.create(engine)
    yield engine
    engine.dispose()


def _add_user(engine, email: str = "judge@justice.gov.uk") -> User:
    with Session(engine) as session:
        user = User(email=email, azure_user_id=str(uuid4()))
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def _segment(blob_path: str) -> str:
    return blob_path.split("/")[1]


@pytest.fixture
def service(db_engine):
    with (
        patch("transcribe_api.stt.work_poller.get_settings"),
        patch("transcribe_api.stt.work_poller.AsyncAzureBlobManager"),
        patch("transcribe_api.stt.work_poller.engine", db_engine),
    ):
        svc = TranscriptionPollingService()
        svc.azure_blob_manager.get_blob_metadata = AsyncMock(return_value={})
        svc.azure_blob_manager.set_blob_metadata = AsyncMock(return_value=True)
        yield svc


def _metadata_writes(svc) -> list[dict]:
    return [c.kwargs["metadata"] for c in svc.azure_blob_manager.set_blob_metadata.call_args_list]


class TestResolveBlobOwner:
    def test_oid_upload_path_round_trips_to_its_user(self, db_engine):
        user = _add_user(db_engine)
        _add_user(db_engine, email="someone.else@justice.gov.uk")
        blob_path = get_file_blob_path(str(user.azure_user_id), f"{uuid4()}.mp4")

        with Session(db_engine) as session:
            owner = resolve_blob_owner(session, _segment(blob_path))

        assert owner is not None
        assert owner.id == user.id

    @pytest.mark.parametrize("segment", ["judge@justice.gov.uk", "Judge@Justice.GOV.uk"])
    def test_legacy_email_path_resolves_case_insensitively(self, db_engine, segment):
        user = _add_user(db_engine, email="judge@justice.gov.uk")

        with Session(db_engine) as session:
            owner = resolve_blob_owner(session, segment)

        assert owner is not None
        assert owner.id == user.id

    def test_stored_email_case_does_not_matter(self, db_engine):
        user = _add_user(db_engine, email="Judge@Justice.gov.uk")

        with Session(db_engine) as session:
            owner = resolve_blob_owner(session, "judge@justice.gov.uk")

        assert owner is not None
        assert owner.id == user.id

    def test_duplicate_email_rows_fail_explicitly(self, db_engine):
        _add_user(db_engine, email="judge@justice.gov.uk")
        _add_user(db_engine, email="JUDGE@justice.gov.uk")

        with Session(db_engine) as session, pytest.raises(AmbiguousBlobOwnerError):
            resolve_blob_owner(session, "judge@justice.gov.uk")

    def test_unknown_oid_returns_none(self, db_engine):
        _add_user(db_engine)

        with Session(db_engine) as session:
            assert resolve_blob_owner(session, str(uuid4())) is None

    def test_segment_without_at_is_never_matched_by_email(self, db_engine):
        # An email column that happens to hold a non-email value must not be
        # reachable through the email fallback: only segments containing "@" are emails.
        _add_user(db_engine, email="not-an-email")

        with Session(db_engine) as session:
            assert resolve_blob_owner(session, "not-an-email") is None


@pytest.mark.asyncio
class TestProcessDiscoveredAudio:
    async def test_oid_upload_is_transcribed_for_its_owner(self, service, db_engine):
        user = _add_user(db_engine)
        transcription_id = str(uuid4())
        blob_path = get_file_blob_path(str(user.azure_user_id), f"{transcription_id}.mp4")

        with patch(
            "transcribe_api.stt.work_poller.transcribe_and_generate_llm_output", new_callable=AsyncMock
        ) as transcribe:
            result = await service.process_discovered_audio({"name": blob_path, "metadata": {}})

        assert result is True
        transcribe.assert_awaited_once()
        kwargs = transcribe.await_args.kwargs
        assert kwargs["user_id"] == user.id
        assert kwargs["azure_user_id"] == str(user.azure_user_id)
        assert kwargs["user_upload_blob_storage_file_key"] == blob_path
        assert kwargs["transcription_id"] == transcription_id
        writes = _metadata_writes(service)
        assert writes[-1]["processed"] == "true"
        assert all(w.get("status") != "retrying" for w in writes)

    async def test_legacy_mixed_case_email_upload_is_transcribed(self, service, db_engine):
        user = _add_user(db_engine, email="judge@justice.gov.uk")
        blob_path = get_file_blob_path("Judge@Justice.gov.uk", f"{uuid4()}.webm")

        with patch(
            "transcribe_api.stt.work_poller.transcribe_and_generate_llm_output", new_callable=AsyncMock
        ) as transcribe:
            result = await service.process_discovered_audio({"name": blob_path, "metadata": {}})

        assert result is True
        assert transcribe.await_args.kwargs["user_id"] == user.id
        assert _metadata_writes(service)[-1]["processed"] == "true"

    async def test_ambiguous_email_owner_is_not_transcribed(self, service, db_engine):
        _add_user(db_engine, email="judge@justice.gov.uk")
        _add_user(db_engine, email="judge@justice.gov.uk")
        blob_path = get_file_blob_path("judge@justice.gov.uk", f"{uuid4()}.mp4")

        with patch(
            "transcribe_api.stt.work_poller.transcribe_and_generate_llm_output", new_callable=AsyncMock
        ) as transcribe:
            result = await service.process_discovered_audio({"name": blob_path, "metadata": {}})

        assert result is False
        transcribe.assert_not_awaited()
        last = _metadata_writes(service)[-1]
        assert last["status"] == "retrying"
        assert "ambiguous" in last["last_error"].lower()
        # The error stored on the blob must not echo the raw owner segment (PII).
        assert "judge@justice.gov.uk" not in last["last_error"]

    async def test_unknown_owner_retries_then_fails_permanently(self, service, db_engine):
        _add_user(db_engine)
        blob_path = get_file_blob_path(str(uuid4()), f"{uuid4()}.mp4")
        stored: dict = {}

        async def get_metadata(_name):
            return dict(stored)

        async def set_metadata(blob_name, metadata):
            stored.clear()
            stored.update(metadata)
            return True

        service.azure_blob_manager.get_blob_metadata = AsyncMock(side_effect=get_metadata)
        service.azure_blob_manager.set_blob_metadata = AsyncMock(side_effect=set_metadata)

        with patch(
            "transcribe_api.stt.work_poller.transcribe_and_generate_llm_output", new_callable=AsyncMock
        ) as transcribe:
            for attempt in (1, 2):
                result = await service.process_discovered_audio({"name": blob_path, "metadata": dict(stored)})
                assert result is False
                assert stored["status"] == "retrying"
                assert stored["retry_count"] == str(attempt)

            service.azure_blob_manager.list_blobs_in_prefix = AsyncMock(
                return_value=[{"name": blob_path, "metadata": dict(stored)}]
            )
            assert await service.poll_for_new_audio_files() == []

        transcribe.assert_not_awaited()
        assert stored["status"] == "permanently_failed"


def _ownership_accepts(blob_path: str, user: User) -> bool:
    from transcribe_api.api.routes_dictation import _validate_blob_ownership

    request = MagicMock()
    request.method = "GET"
    request.url.path = "/api/download"
    try:
        _validate_blob_ownership(blob_path, user, request)
    except HTTPException as exc:
        assert exc.status_code == 403
        return False
    return True


@pytest.mark.parametrize(
    ("segment_for", "expected"),
    [
        (lambda me, other: str(me.azure_user_id), True),
        (lambda me, other: me.email, True),
        (lambda me, other: me.email.upper(), True),
        (lambda me, other: str(other.azure_user_id), False),
        (lambda me, other: other.email, False),
        (lambda me, other: str(uuid4()), False),
    ],
    ids=["oid", "email", "mixed-case-email", "foreign-oid", "foreign-email", "unknown-oid"],
)
def test_poller_resolver_and_ownership_check_agree(db_engine, segment_for, expected):
    me = _add_user(db_engine, email="judge@justice.gov.uk")
    other = _add_user(db_engine, email="other.judge@justice.gov.uk")
    segment = segment_for(me, other)
    blob_path = get_file_blob_path(segment, f"{uuid4()}.mp4")

    with Session(db_engine) as session:
        owner = resolve_blob_owner(session, Path(blob_path).parts[1])
    poller_says_mine = owner is not None and owner.id == me.id

    assert poller_says_mine is expected
    assert _ownership_accepts(blob_path, me) is expected
