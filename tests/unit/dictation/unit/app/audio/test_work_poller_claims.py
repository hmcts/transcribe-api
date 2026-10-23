"""Startup cleanup and claiming in the dictation upload poller, against an in-memory blob store.

The fake replaces the Azure SDK's async BlobServiceClient, so the real
AsyncAzureBlobManager and TranscriptionPollingService run unmodified. Like Azure, it
gives every blob an ETag that changes on each write and enforces If-Match
(etag + MatchConditions.IfNotModified) by raising ResourceModifiedError, so two
pollers racing for the same upload behave as they would against real storage.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

from transcribe_api.stt.work_poller import STALE_CLAIM_AFTER, TranscriptionPollingService

OWNER = str(uuid4())


def upload_path(ext: str = "mp4") -> str:
    return f"user-uploads/{OWNER}/{uuid4()}.{ext}"


class FakeBlobStore:
    """Blob name -> {metadata, etag, last_modified}, with Azure's If-Match semantics."""

    def __init__(self):
        self.blobs: dict[str, dict] = {}
        self._etags = count(1)

    def put(self, name: str, metadata: dict | None = None) -> None:
        self.blobs[name] = {"metadata": dict(metadata or {}), "etag": "", "last_modified": None}
        self._touch(name)

    def _touch(self, name: str) -> None:
        self.blobs[name]["etag"] = f'"0x{next(self._etags)}"'
        self.blobs[name]["last_modified"] = datetime.now(UTC)

    def _check(self, name: str, etag: str | None, match_condition) -> dict:
        if name not in self.blobs:
            raise ResourceNotFoundError("BlobNotFound")
        blob = self.blobs[name]
        if match_condition == MatchConditions.IfNotModified and etag != blob["etag"]:
            raise ResourceModifiedError("ConditionNotMet")
        return blob

    def metadata(self, name: str) -> dict:
        return self.blobs[name]["metadata"]


class FakeBlobClient:
    def __init__(self, store: FakeBlobStore, name: str):
        self.store = store
        self.name = name

    async def get_blob_properties(self):
        await asyncio.sleep(0)  # yield, so concurrent callers interleave read-then-write
        blob = self.store._check(self.name, None, None)
        return SimpleNamespace(
            name=self.name, metadata=dict(blob["metadata"]), etag=blob["etag"], last_modified=blob["last_modified"]
        )

    async def set_blob_metadata(self, metadata=None, *, etag=None, match_condition=None, **_):
        await asyncio.sleep(0)
        blob = self.store._check(self.name, etag, match_condition)
        blob["metadata"] = dict(metadata or {})
        self.store._touch(self.name)

    async def delete_blob(self, delete_snapshots=None, *, etag=None, match_condition=None, **_):
        await asyncio.sleep(0)
        self.store._check(self.name, etag, match_condition)
        del self.store.blobs[self.name]


class FakeContainerClient:
    def __init__(self, store: FakeBlobStore):
        self.store = store

    async def list_blobs(self, name_starts_with="", include=None):
        for name, blob in list(self.store.blobs.items()):
            if name.startswith(name_starts_with):
                yield SimpleNamespace(
                    name=name,
                    metadata=dict(blob["metadata"]) if include else None,
                    etag=blob["etag"],
                    last_modified=blob["last_modified"],
                    size=1,
                )


class FakeBlobServiceClient:
    def __init__(self, store: FakeBlobStore):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def get_blob_client(self, container, blob):
        return FakeBlobClient(self.store, blob)

    def get_container_client(self, container):
        return FakeContainerClient(self.store)


@pytest.fixture
def store():
    return FakeBlobStore()


@pytest.fixture
def transcribe():
    with patch("transcribe_api.stt.work_poller.transcribe_and_generate_llm_output", new_callable=AsyncMock) as mock:
        yield mock


@pytest.fixture
def make_poller(store, transcribe):
    """Build pollers that share one fake storage account (one per simulated worker pod)."""
    blob_settings = MagicMock(AZURE_STORAGE_ACCOUNT_NAME="acct", AZURE_STORAGE_CONTAINER_NAME="audio")
    credential = MagicMock(close=AsyncMock())
    owner = SimpleNamespace(id=uuid4(), email="judge@justice.gov.uk", azure_user_id=OWNER)
    with (
        patch("transcribe_api.runtime.blob_dictation.get_settings", return_value=blob_settings),
        patch("transcribe_api.runtime.blob_dictation.DefaultAzureCredential", return_value=credential),
        patch(
            "transcribe_api.runtime.blob_dictation.AsyncBlobServiceClient",
            side_effect=lambda **_: FakeBlobServiceClient(store),
        ),
        patch("transcribe_api.stt.work_poller.get_settings"),
    ):

        def _make() -> TranscriptionPollingService:
            poller = TranscriptionPollingService()
            poller.resolve_blob_owner_from_path = MagicMock(return_value=owner)
            return poller

        yield _make


async def run_cycle(poller: TranscriptionPollingService) -> list[str]:
    """One iteration of run_polling_loop, with the queued blobs processed inline."""
    discovered = await poller.poll_cycle()
    for blob in discovered:
        await poller.process_discovered_audio(blob)
    return [blob["name"] for blob in discovered]


def claim(started: datetime, retry_count: str = "0") -> dict:
    return {
        "processed": "false",
        "status": "in_progress",
        "started_at": started.isoformat(),
        "retry_count": retry_count,
    }


@pytest.mark.asyncio
class TestStartupNeverLosesUploads:
    async def test_unclaimed_upload_survives_startup_and_is_transcribed(self, store, make_poller, transcribe):
        # Uploads arrive with no metadata; the claim is the first metadata write.
        path = upload_path()
        store.put(path)

        processed = await run_cycle(make_poller())

        assert path in store.blobs, "startup deleted an upload it had never processed"
        assert processed == [path]
        transcribe.assert_awaited_once()
        assert transcribe.await_args.kwargs["user_upload_blob_storage_file_key"] == path
        assert store.metadata(path)["processed"] == "true"

    async def test_retrying_and_failed_uploads_are_kept(self, store, make_poller):
        retrying = upload_path()
        failed = upload_path()
        store.put(retrying, {"processed": "false", "status": "retrying", "retry_count": "1"})
        store.put(failed, {"processed": "false", "status": "permanently_failed", "retry_count": "2"})

        await make_poller()._cleanup_old_blobs_on_startup()

        assert retrying in store.blobs
        assert failed in store.blobs


@pytest.mark.asyncio
class TestStartupDeletion:
    """The only deletion left: audio already marked processed (its transcript exists)."""

    async def test_deletes_only_processed_audio(self, store, make_poller):
        processed = upload_path()
        unclaimed = upload_path("webm")
        claimed = upload_path()
        submission = f"user-uploads/{OWNER}/live-transcription-submission-{uuid4()}.json"
        store.put(processed, {"processed": "true", "processed_at": datetime.now(UTC).isoformat()})
        store.put(unclaimed)
        store.put(claimed, claim(datetime.now(UTC)))
        store.put(submission, {"processed": "true"})  # not audio: never touched

        await make_poller()._cleanup_old_blobs_on_startup()

        assert set(store.blobs) == {unclaimed, claimed, submission}

    async def test_processed_audio_changed_since_listing_is_not_deleted(self, store, make_poller):
        path = upload_path()
        store.put(path, {"processed": "true"})
        poller = make_poller()
        listed = await poller.azure_blob_manager.list_blobs_in_prefix("user-uploads/")
        store.put(path)  # overwritten (new ETag) between listing and delete

        deleted = await poller.azure_blob_manager.delete_blob(path, etag=listed[0]["etag"])

        assert deleted is False
        assert path in store.blobs


@pytest.mark.asyncio
class TestStartupClaims:
    async def test_fresh_claim_is_left_alone(self, store, make_poller, transcribe):
        # Claimed moments ago by the other pod of a rolling update, which is still working on it.
        path = upload_path()
        store.put(path, claim(datetime.now(UTC) - timedelta(minutes=5)))
        before = dict(store.blobs[path])

        processed = await run_cycle(make_poller())

        assert processed == []
        assert store.blobs[path] == before, "startup rewrote a claim held by a live worker"
        transcribe.assert_not_awaited()

    async def test_stale_claim_is_reset_and_processed(self, store, make_poller, transcribe):
        path = upload_path()
        store.put(path, claim(datetime.now(UTC) - STALE_CLAIM_AFTER - timedelta(minutes=1), retry_count="1"))

        processed = await run_cycle(make_poller())

        assert processed == [path]
        transcribe.assert_awaited_once()
        assert store.metadata(path)["processed"] == "true"

    async def test_claim_that_goes_stale_after_startup_is_reclaimed(self, store, make_poller, transcribe):
        # A pod killed mid-transcription (e.g. the old pod of a rolling update) leaves a
        # claim behind; it must be recovered once stale, not only at the next restart.
        poller = make_poller()
        await run_cycle(poller)  # startup cycle on an empty store
        path = upload_path()
        store.put(path, claim(datetime.now(UTC) - STALE_CLAIM_AFTER - timedelta(minutes=1)))

        processed = await run_cycle(poller)

        assert processed == [path]
        transcribe.assert_awaited_once()

    async def test_stale_reclaim_keeps_retry_count(self, store, make_poller):
        path = upload_path()
        store.put(path, claim(datetime.now(UTC) - STALE_CLAIM_AFTER - timedelta(minutes=1), retry_count="1"))

        assert await make_poller()._mark_blob_in_progress(path) is True

        assert store.metadata(path)["status"] == "in_progress"
        assert store.metadata(path)["retry_count"] == "1"


@pytest.mark.asyncio
class TestAtomicClaim:
    async def test_two_pollers_racing_for_one_upload_only_one_claims(self, store, make_poller):
        path = upload_path()
        store.put(path)

        results = await asyncio.gather(
            make_poller()._mark_blob_in_progress(path), make_poller()._mark_blob_in_progress(path)
        )

        assert sorted(results) == [False, True]
        assert store.metadata(path)["status"] == "in_progress"

    async def test_two_pollers_racing_transcribe_the_upload_once(self, store, make_poller, transcribe):
        path = upload_path()
        store.put(path)
        first, second = make_poller(), make_poller()
        blob = {"name": path, "metadata": {}}

        results = await asyncio.gather(first.process_discovered_audio(blob), second.process_discovered_audio(blob))

        assert sorted(results) == [False, True]
        transcribe.assert_awaited_once()
        assert store.metadata(path)["processed"] == "true"
        assert "retry_count" not in store.metadata(path), "the losing poller recorded a failed attempt"

    async def test_two_pollers_racing_for_a_stale_claim_only_one_reclaims(self, store, make_poller):
        path = upload_path()
        store.put(path, claim(datetime.now(UTC) - STALE_CLAIM_AFTER - timedelta(minutes=1)))

        results = await asyncio.gather(
            make_poller()._mark_blob_in_progress(path), make_poller()._mark_blob_in_progress(path)
        )

        assert sorted(results) == [False, True]

    async def test_claim_is_conditional_on_the_etag_it_read(self, store, make_poller):
        path = upload_path()
        store.put(path)
        poller = make_poller()
        real_set = poller.azure_blob_manager.set_blob_metadata
        poller.azure_blob_manager.set_blob_metadata = AsyncMock(side_effect=real_set)

        await poller._mark_blob_in_progress(path)

        assert poller.azure_blob_manager.set_blob_metadata.await_args.kwargs["etag"] is not None

    async def test_queued_duplicate_of_a_processed_upload_is_not_reprocessed(self, store, make_poller, transcribe):
        # Polls every 30s can queue the same upload more than once before a worker claims it.
        path = upload_path()
        store.put(path)
        poller = make_poller()
        blob = {"name": path, "metadata": {}}

        assert await poller.process_discovered_audio(blob) is True
        assert await poller.process_discovered_audio(blob) is False

        transcribe.assert_awaited_once()
        assert store.metadata(path)["processed"] == "true"
