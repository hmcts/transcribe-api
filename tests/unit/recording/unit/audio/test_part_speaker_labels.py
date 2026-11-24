"""DIAAT-408: speaker labels of a recording transcribed in parts.

Diarization is per file: "Speaker 0" in part 2 is not known to be "Speaker 0" in part 1, so the
speakers of each part are labelled with their part ("Part 2 · Speaker 0") and are never joined across
parts. A recording in one piece is labelled exactly as before. The existing speaker editor (rename one
section, or every section by that speaker) is enough to map the parts' speakers onto roles: renaming
several to the same name merges them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.unit.jurisdiction_fixtures import IAC_JURISDICTION_ID
from tests.unit.recording.unit.async_upload_helpers import USER_ID, new_engine, reload_job
from tests.unit.recording.unit.audio.multipart_helpers import prepare as prepare_job
from tests.unit.recording.unit.audio.multipart_helpers import scale_down, use_engine, waiting
from transcribe_api.api.app import create_app
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
from transcribe_api.domain.auth.dependencies_recording import get_current_user
from transcribe_api.domain.models_recording import DialogueEntry, JobStatus, User
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.stt import polling_service, speakers_recording
from transcribe_api.stt.part_merge import PartResult, merge_parts, part_speaker_prefix
from transcribe_api.stt.polling_service import BatchPollingService
from transcribe_api.stt.speakers_recording import process_speakers


def _entry(speaker: str, text: str, start: float) -> DialogueEntry:
    return DialogueEntry(speaker=speaker, text=text, start_time=start, end_time=start + 1.0, confidence=0.9)


def test_the_prefix_names_the_part_and_is_padded_so_that_the_labels_sort_in_part_order():
    assert part_speaker_prefix(1, 3) == "Part 1 · "
    assert part_speaker_prefix(3, 3) == "Part 3 · "
    assert part_speaker_prefix(2, 12) == "Part 02 · "
    labels = [f"{part_speaker_prefix(i, 12)}Speaker 0" for i in range(1, 13)]
    assert labels == sorted(labels)


def test_a_transcript_in_one_piece_is_labelled_exactly_as_before():
    entries = [_entry("1", "Good morning.", 0.0), _entry("2", "Morning.", 2.0), _entry("1", "Please sit.", 4.0)]
    assert [e.speaker for e in process_speakers(entries)] == ["Speaker 0", "Speaker 1", "Speaker 0"]
    assert process_speakers(entries) == process_speakers(entries, label_prefix=None)


def test_a_prefix_labels_the_parts_speakers_in_order_of_appearance_within_the_part():
    entries = [_entry("2", "Good morning.", 0.0), _entry("1", "Morning.", 2.0)]
    assert [e.speaker for e in process_speakers(entries, label_prefix="Part 2 · ")] == [
        "Part 2 · Speaker 0",
        "Part 2 · Speaker 1",
    ]


def _part(index: int, count: int, entries: list[DialogueEntry], offset: float) -> PartResult:
    return PartResult(index, count, offset, 100.0, entries, None)


def test_the_speaker_who_carries_on_across_a_cut_is_not_joined_to_themselves_in_the_next_part():
    # Both parts' diarization calls the speaker around the cut "1": it is not known to be the same person.
    one = _part(
        1,
        2,
        [_entry("1", "Counsel opens.", 0.0), _entry("2", "Judge asks.", 3.0), _entry("1", "Counsel answers.", 6.0)],
        0.0,
    )
    two = _part(2, 2, [_entry("1", "and continues.", 0.0), _entry("2", "The judge replies.", 3.0)], 100.0)

    merged = merge_parts([two, one])  # given out of order: the result is in part order

    assert [(e.speaker, e.text) for e in merged] == [
        ("Part 1 · Speaker 0", "Counsel opens."),
        ("Part 1 · Speaker 1", "Judge asks."),
        ("Part 1 · Speaker 0", "Counsel answers."),
        ("Part 2 · Speaker 0", "and continues."),  # a new entry, not appended to "Counsel answers."
        ("Part 2 · Speaker 1", "The judge replies."),
    ]
    assert [e.start_time for e in merged] == [0.0, 3.0, 6.0, 100.0, 103.0]


def test_the_labels_survive_a_failed_labelling_step_still_apart_by_part(mocker):
    mocker.patch.object(speakers_recording, "add_speaker_labels", side_effect=RuntimeError("boom"))
    merged = merge_parts([_part(1, 2, [_entry("1", "a", 0.0)], 0.0), _part(2, 2, [_entry("1", "b", 0.0)], 100.0)])
    assert [e.speaker for e in merged] == ["Part 1 · 0", "Part 2 · 0"]


# Through the real assembly, and the speaker editor -------------------------------------------------


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch, mocker):
    scale_down(monkeypatch, mocker, tmp_path)


@pytest.fixture
def engine(mocker, tmp_path):
    engine = new_engine(tmp_path / "jobs.db")
    use_engine(mocker, engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine):
    app = create_app()
    user = AuthenticatedUser(
        db_user=User(id=USER_ID, azure_user_id="oid", email="judge@example.test", role="Judge"),
        ctx=Context("IAC", "Judge"),
    )
    app.dependency_overrides[get_current_user] = lambda: user

    def session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = session
    return TestClient(app, raise_server_exceptions=False)


async def _assembled_job(engine):
    job = waiting(engine, jurisdiction_id=IAC_JURISDICTION_ID)
    await prepare_job()
    await _poll(engine)
    assert reload_job(engine, job.id).status == JobStatus.SUCCEEDED
    return job


async def _poll(engine):
    from unittest.mock import AsyncMock

    poller = BatchPollingService(webhook_secret_resolver=lambda _c: "")
    poller._dispatch_success = AsyncMock()
    await poller._poll_parts_once()


async def test_the_assembled_transcript_labels_every_speaker_with_its_part(engine):
    job = await _assembled_job(engine)
    entries = reload_job(engine, job.id).dialogue_entries
    assert [e["speaker"] for e in entries] == [f"Part {k} · Speaker {n}" for k in (1, 2, 3) for n in (0, 1)]
    assert polling_service  # (the poller built them: see test_multipart_polling for the offsets)


def _ids(client, job_id) -> dict[str, str]:
    """speaker -> the id of its first section, as the API gives them."""
    entries = client.get(f"/api/v1/jobs/{job_id}").json()["dialogue_entries"]
    first: dict[str, str] = {}
    for entry in entries:
        first.setdefault(entry["speaker"], entry["id"])
    return first


def _speakers(client, job_id) -> list[str]:
    return [e["speaker"] for e in client.get(f"/api/v1/jobs/{job_id}").json()["dialogue_entries"]]


async def test_the_speaker_editor_renames_one_speaker_of_one_part_without_touching_the_others(client, engine):
    job = await _assembled_job(engine)
    ids = _ids(client, job.id)

    response = client.patch(
        f"/api/v1/jobs/{job.id}/segments/{ids['Part 2 · Speaker 1']}/speaker",
        json={"speaker": "Respondent", "apply_to_all": True},
    )

    assert response.status_code == 200
    assert _speakers(client, job.id) == [
        "Part 1 · Speaker 0", "Part 1 · Speaker 1", "Part 2 · Speaker 0", "Respondent", "Part 3 · Speaker 0", "Part 3 · Speaker 1",
    ]  # fmt: skip


async def test_the_same_person_in_every_part_is_one_speaker_once_each_is_renamed_to_the_same_name(client, engine):
    job = await _assembled_job(engine)
    ids = _ids(client, job.id)
    for part in (1, 2, 3):  # the judge is "Speaker 0" in each part
        response = client.patch(
            f"/api/v1/jobs/{job.id}/segments/{ids[f'Part {part} · Speaker 0']}/speaker",
            json={"speaker": "Judge", "apply_to_all": True},
        )
        assert response.status_code == 200
    speakers = _speakers(client, job.id)
    assert speakers.count("Judge") == 3
    assert [s for s in speakers if s != "Judge"] == ["Part 1 · Speaker 1", "Part 2 · Speaker 1", "Part 3 · Speaker 1"]
    # And the whole speaker is renamed: its further sections too (apply to all), not only the one clicked.
    again = _ids(client, job.id)
    assert set(again) == {"Judge", "Part 1 · Speaker 1", "Part 2 · Speaker 1", "Part 3 · Speaker 1"}
