"""The recording blob manager on both stand-ins for Azure Blob Storage (tests/unit/recording/blob_stores.py).

Every scenario runs on the in-memory fake the unit tests use and on Azurite: one that passes on Azurite and fails
on the fake means the fake no longer behaves like storage, and the unit tests built on it can no longer be trusted.
"""

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager


@pytest.fixture(params=["memory", pytest.param("azurite", marks=pytest.mark.azurite)])
def storage(request):
    return request.getfixturevalue(f"{request.param}_blob_storage")


async def test_bytes_written_are_read_back_whole_and_in_ranges(storage):
    async with AsyncAzureBlobManager() as manager:
        assert await manager.create_blob_from_bytes(b"0123456789", "uploads/u1/a.wav") is True
        assert await manager.get_blob_size("uploads/u1/a.wav") == 10
        assert await manager.download_blob_range("uploads/u1/a.wav", 2, 3) == b"234"
        assert await manager.download_blob_range("uploads/u1/a.wav", 8, 100) == b"89"
    assert storage.get("uploads/u1/a.wav") == b"0123456789"


async def test_a_file_is_uploaded_whole(storage, tmp_path):
    content = bytes(range(256)) * 400
    path = tmp_path / "recording.wav"
    path.write_bytes(content)

    async with AsyncAzureBlobManager() as manager:
        assert await manager.create_blob_from_file(path, "uploads/u1/recording.wav", max_concurrency=2) is True
    assert storage.get("uploads/u1/recording.wav") == content


async def test_a_file_that_is_not_there_is_not_uploaded(storage, tmp_path):
    async with AsyncAzureBlobManager() as manager:
        assert await manager.create_blob_from_file(tmp_path / "missing.wav", "uploads/u1/missing.wav") is False
    assert "uploads/u1/missing.wav" not in storage


async def test_writing_again_replaces_the_blob(storage):
    storage.put("uploads/u1/a.wav", b"first")
    async with AsyncAzureBlobManager() as manager:
        assert await manager.create_blob_from_bytes(b"second, longer", "uploads/u1/a.wav") is True
        assert await manager.get_blob_size("uploads/u1/a.wav") == len(b"second, longer")
    assert storage.get("uploads/u1/a.wav") == b"second, longer"


async def test_a_blob_that_is_not_there_has_no_size_range_or_metadata(storage):
    async with AsyncAzureBlobManager() as manager:
        assert await manager.blob_exists("uploads/u1/none.wav") is False
        assert await manager.get_blob_size("uploads/u1/none.wav") is None
        assert await manager.download_blob_range("uploads/u1/none.wav", 0, 10) is None
        assert await manager.get_blob_metadata("uploads/u1/none.wav") == {}
        assert await manager.set_blob_metadata("uploads/u1/none.wav", {"owner": "u1"}) is False


# Not the end itself: Azurite answers an empty range there, where HTTP refuses it; playback refuses it first.
@pytest.mark.parametrize("start", [11, 25])
async def test_a_range_starting_past_the_end_is_refused(storage, start):
    storage.put("uploads/u1/a.wav", b"0123456789")
    async with AsyncAzureBlobManager() as manager:
        with pytest.raises(HttpResponseError) as refused:
            await manager.download_blob_range("uploads/u1/a.wav", start, 5)
    assert refused.value.status_code == 416


async def test_an_empty_blob_has_a_size_and_no_range(storage):
    storage.put("uploads/u1/empty.wav", b"")
    async with AsyncAzureBlobManager() as manager:
        assert await manager.get_blob_size("uploads/u1/empty.wav") == 0
        with pytest.raises(HttpResponseError) as refused:
            await manager.download_blob_range("uploads/u1/empty.wav", 0, 10)
    assert refused.value.status_code == 416


async def test_a_range_is_streamed_in_order(storage):
    content = bytes(range(256)) * 64
    storage.put("uploads/u1/a.wav", content)

    chunks = [chunk async for chunk in AsyncAzureBlobManager().stream_blob_range("uploads/u1/a.wav", 100, 9000)]

    assert b"".join(chunks) == content[100:9100]


async def test_a_blob_is_downloaded_to_a_file_and_one_not_there_is_not_found(storage, tmp_path):
    storage.put("incoming/u1/upload.mp3", b"original audio")
    async with AsyncAzureBlobManager() as manager:
        assert await manager.download_blob_to_file("incoming/u1/upload.mp3", tmp_path / "copy.mp3") is True
        with pytest.raises(FileNotFoundError):
            await manager.download_blob_to_file("incoming/u1/none.mp3", tmp_path / "none.mp3")
    assert (tmp_path / "copy.mp3").read_bytes() == b"original audio"


async def test_delete_removes_the_blob_and_reports_one_already_gone(storage):
    storage.put("uploads/u1/a.wav", b"audio")
    async with AsyncAzureBlobManager() as manager:
        assert await manager.delete_blob("uploads/u1/a.wav") is True
        assert await manager.blob_exists("uploads/u1/a.wav") is False
        assert await manager.delete_blob("uploads/u1/a.wav") is False
    assert storage.names() == []


async def test_names_under_a_prefix_are_listed_in_name_order(storage):
    for name in ("uploads/u1/d.wav", "incoming/u10/c.wav", "incoming/u1/b.mp3", "incoming/u1/a.wav"):
        storage.put(name, b"x")

    async with AsyncAzureBlobManager() as manager:
        under_folder = await manager.find_blob_names("incoming/u1/")
        under_prefix = await manager.find_blob_names("incoming/u1")
        nothing = await manager.find_blob_names("incoming/u2/")

    assert under_folder == ["incoming/u1/a.wav", "incoming/u1/b.mp3"]
    assert under_prefix == ["incoming/u1/a.wav", "incoming/u1/b.mp3", "incoming/u10/c.wav"]
    assert nothing == []


async def test_a_listing_carries_sizes_and_metadata(storage):
    storage.put("parts/j1/part-1.wav", b"12345", metadata={"part": "1"})
    storage.put("parts/j1/part-2.wav", b"123", metadata={"part": "2"})

    async with AsyncAzureBlobManager() as manager:
        listed = await manager.list_blobs_in_prefix("parts/j1/")

    assert [(blob["name"], blob["size"], blob["metadata"]) for blob in listed] == [
        ("parts/j1/part-1.wav", 5, {"part": "1"}),
        ("parts/j1/part-2.wav", 3, {"part": "2"}),
    ]


async def test_metadata_is_replaced_whole(storage):
    storage.put("uploads/u1/a.wav", b"audio", metadata={"owner": "u1", "stage": "uploaded"})
    async with AsyncAzureBlobManager() as manager:
        assert await manager.get_blob_metadata("uploads/u1/a.wav") == {"owner": "u1", "stage": "uploaded"}
        assert await manager.set_blob_metadata("uploads/u1/a.wav", {"owner": "u2"}) is True
        assert await manager.get_blob_metadata("uploads/u1/a.wav") == {"owner": "u2"}


async def test_only_an_existing_container_passes_the_health_check(storage):
    async with AsyncAzureBlobManager() as manager:
        await manager.check_container()
        with pytest.raises(ResourceNotFoundError):
            await manager.check_container("no-such-container")


async def test_nothing_is_written_to_a_container_that_is_not_there(storage):
    async with AsyncAzureBlobManager() as manager:
        assert await manager.create_blob_from_bytes(b"x", "a.wav", container_name="no-such-container") is False
        assert await manager.blob_exists("a.wav", container_name="no-such-container") is False
