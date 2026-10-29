"""DIAAT-286: AI provenance, stored with the output in the application database.

AC2: the configuration used to generate a transcript or a decision document can be
identified (``transcriptionjob.provenance``) without storing content in monitoring.
"""

import asyncio
import contextvars
import json
import threading
import uuid
from datetime import datetime
from importlib import metadata
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hmcts_otel.testing import capture_telemetry
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from transcribe_api.documents import content_loader, versioning
from transcribe_api.domain.provenance_dictation import Provenance, endpoint_reference

# The template version of one render -------------------------------------------------------


def _render(content: dict, *, before_returning: threading.Event, wait_for: threading.Event | None = None) -> None:
    """A render: loads the document content, (lets the other render run), returns."""
    content_loader._used(content)
    before_returning.set()
    if wait_for is not None:
        assert wait_for.wait(timeout=2)


@pytest.mark.asyncio
async def test_concurrent_renders_each_get_the_version_of_their_own_content():
    """Render A loads its content, render B loads different content (an admin edit in
    between), then A finishes. A process-wide "last used" value gives A the version of
    B's content; each render must get its own."""
    content_a, content_b = {"intro": "before the edit"}, {"intro": "after the edit"}
    a_loaded, b_loaded = threading.Event(), threading.Event()

    async def render_a():
        with versioning.track_render() as render:
            await asyncio.to_thread(_render, content_a, before_returning=a_loaded, wait_for=b_loaded)
        return render.version

    async def render_b():
        await asyncio.to_thread(a_loaded.wait, 2)
        with versioning.track_render() as render:
            await asyncio.to_thread(_render, content_b, before_returning=b_loaded)
        return render.version

    version_a, version_b = await asyncio.gather(render_a(), render_b())

    with versioning.track_render() as alone_a:
        content_loader._used(content_a)
    with versioning.track_render() as alone_b:
        content_loader._used(content_b)
    assert version_a == alone_a.version
    assert version_b == alone_b.version
    assert version_a != version_b
    # The process-wide value (template_version) is B's: what A used to get.
    assert versioning.template_version() == version_b


def test_template_version_still_reports_the_last_content_used():
    content_loader._used({"intro": "x"})
    with versioning.track_render() as render:
        content_loader._used({"intro": "x"})
    assert versioning.template_version() == render.version


def test_content_changed_during_a_render_gives_a_version_of_its_own():
    with versioning.track_render() as render:
        content_loader._used({"intro": "one"})
        content_loader._used({"intro": "two"})
    with versioning.track_render() as one:
        content_loader._used({"intro": "one"})
    assert render.version not in {one.version, "unknown"}


# Speech configuration ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://uksouth.api.cognitive.microsoft.com/", ("uksouth.api.cognitive.microsoft.com", "uksouth")),
        ("https://hmcts-speech.cognitiveservices.azure.com", ("hmcts-speech.cognitiveservices.azure.com", None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_the_endpoint_reference_is_the_host_and_region_only(endpoint, expected):
    assert endpoint_reference(endpoint) == expected


@pytest.mark.asyncio
async def test_the_recorded_speech_configuration_is_the_one_sent(tmp_path):
    """One definition for the request and its provenance: they cannot drift apart."""
    from transcribe_api.stt import fast_client as transcription

    settings = MagicMock(AZURE_SPEECH_KEY="key", AZURE_SPEECH_ENDPOINT="https://uksouth.api.cognitive.microsoft.com")
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "phrases": [{"speaker": 1, "text": "x", "offsetMilliseconds": 0, "durationMilliseconds": 1}]
    }
    http = MagicMock()
    http.post = AsyncMock(return_value=response)
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(transcription, "get_settings", return_value=settings),
        patch.object(transcription.httpx, "AsyncClient", return_value=http),
        patch.object(transcription, "convert_input_dialogue_entries_to_dialogue_entries", return_value=[]),
    ):
        await transcription.transcribe_audio_with_azure(audio)
        recorded = transcription.speech_configuration()

    url = http.post.call_args.args[0]
    sent = json.loads(http.post.call_args.kwargs["files"]["definition"][1])
    assert url.endswith(f"?api-version={recorded.api_version}")
    assert sent == {
        "locales": recorded.locales,
        "diarization": {"enabled": recorded.diarization},
        "profanityFilterMode": recorded.profanity_filter_mode,
    }
    assert (recorded.endpoint_host, recorded.region, recorded.locales, recorded.diarization) == (
        "uksouth.api.cognitive.microsoft.com",
        "uksouth",
        ["en-GB"],
        True,
    )


# Decision document (live dictation submission) -----------------------------------------


@pytest.fixture
def submission(mocker):
    from tests.unit.dictation.unit.api.test_live_transcription_submission_logging import _patch_submission_deps

    _patch_submission_deps(mocker)
    return mocker.patch("transcribe_api.api.routes_dictation.save_transcription_job")


def test_a_decision_document_records_its_template_version_release_and_time(client, as_user, submission, mocker):
    from tests.unit.dictation.unit.api.test_live_transcription_submission_logging import (
        _MINIMAL_BODY,
        PII_APPELLANT,
        PII_JUDGE,
        PII_RESPONDENT,
        PII_TRANSCRIPT,
        _make_user,
    )

    as_user(_make_user(roles=["Judge"]))
    mocker.patch("transcribe_api.runtime.version.APP_VERSION", "9.9.9-release")
    this_content = {"intro": "the content this render used"}

    def render(**_):
        content_loader._used(this_content)
        # Another request loads different content meanwhile (outside this context).
        contextvars.Context().run(content_loader._used, {"intro": "another request's content"})
        path = MagicMock(spec=Path)
        path.exists.return_value = False
        return path

    mocker.patch("transcribe_api.api.routes_dictation.render_hearing_document", side_effect=render)
    with capture_telemetry() as telemetry:
        assert client.post("/api/live-transcription/submission", json=_MINIMAL_BODY).status_code == 200
        (completed,) = telemetry.events("document_generation.completed")

    job = submission.call_args.args[0]
    provenance = Provenance.model_validate(job.provenance)
    other_requests = versioning.template_version()  # the process-wide "last used"
    with versioning.track_render() as expected:
        content_loader._used(this_content)
    assert provenance.output == "decision_document"
    assert provenance.template == "decision"
    assert provenance.template_version == expected.version == completed["document.template_version"]
    assert provenance.template_version != other_requests
    assert provenance.application_release == "9.9.9-release"
    assert isinstance(provenance.generated_at, datetime)
    assert provenance.speech is None  # dictation: the browser's Speech session, not ours
    text = json.dumps(job.provenance)
    for content in (PII_APPELLANT, PII_RESPONDENT, PII_JUDGE, PII_TRANSCRIPT):
        assert content not in text


@pytest.fixture
def app():
    from fastapi import FastAPI

    import transcribe_api.api.routes_dictation

    test_app = FastAPI()
    test_app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    return test_app


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def as_user(app):
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    yield lambda user: app.dependency_overrides.__setitem__(get_current_user, lambda: user)
    app.dependency_overrides.pop(get_current_user, None)


# Only the server writes provenance -------------------------------------------------------


@pytest.fixture
def db(monkeypatch):
    import tests.unit.dictation.unit.app.database.test_ownership_guard  # noqa: F401 - registers JSONB as JSON on SQLite
    from transcribe_api.domain import interface_dictation as interface_functions

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(interface_functions, "engine", engine)
    return interface_functions


def test_a_client_cannot_set_or_overwrite_provenance(db):
    from transcribe_api.domain.models_dictation import Transcription, TranscriptionJob, User

    with Session(db.engine) as session:
        user = User(email="j@example.com", azure_user_id="oid")
        session.add(user)
        session.commit()
        session.refresh(user)
    transcription = db.save_transcription(Transcription(), user.id)
    real = {"output": "transcript", "application_release": "1.2.3"}
    job = db.save_transcription_job(
        TranscriptionJob(transcription_id=transcription.id, dialogue_entries=[], provenance=real)
    )

    forged = {"output": "transcript", "application_release": "forged"}
    edited = db.save_transcription_job(
        TranscriptionJob(id=job.id, transcription_id=transcription.id, dialogue_entries=[], provenance=forged),
        from_client=True,
    )
    assert edited.provenance == real  # kept, e.g. after a speaker rename
    new = db.save_transcription_job(
        TranscriptionJob(transcription_id=transcription.id, dialogue_entries=[], provenance=forged), from_client=True
    )
    assert new.provenance is None


def test_the_jobs_route_saves_as_a_client(client, as_user, mocker):
    from tests.unit.dictation.unit.api.test_routes import _make_transcription, _make_user

    user = _make_user(roles=["Judge"])
    as_user(user)
    transcription = _make_transcription(user.id)
    mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", return_value=transcription)
    save = mocker.patch("transcribe_api.api.routes_dictation.save_transcription_job", side_effect=lambda job, **_: job)
    body = {"transcription_id": str(transcription.id), "dialogue_entries": [], "provenance": {"output": "forged"}}
    assert client.post(f"/api/transcriptions/{transcription.id}/jobs", json=body).status_code == 200
    assert save.call_args.kwargs["from_client"] is True


# The investigator's view -------------------------------------------------------------------


def test_administrators_see_the_provenance_of_each_output(mocker):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import transcribe_api.api.routes_dictation_privileged
    from tests.unit.dictation.unit.api.test_routes import _make_transcription, _make_user
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user
    from transcribe_api.domain.models_dictation import TranscriptionJob

    test_app = FastAPI()
    test_app.include_router(transcribe_api.api.routes_dictation_privileged.router, prefix="/api")
    test_app.dependency_overrides[get_current_user] = lambda: _make_user(roles=["SystemAdministrator"])
    transcription = _make_transcription(uuid.uuid4())
    record = Provenance(output="decision_document", template="decision", template_version="0123456789ab").to_record()
    job = TranscriptionJob(transcription_id=transcription.id, dialogue_entries=[], provenance=record)
    transcription.__dict__["transcription_jobs"] = [job]
    mocker.patch(
        "transcribe_api.api.routes_dictation_privileged.admin_get_transcription_by_id", return_value=transcription
    )

    body = TestClient(test_app).get(f"/api/admin/transcriptions/{transcription.id}").json()
    assert body["id"] == str(transcription.id)
    assert body["title"] == transcription.title
    assert body["provenance"] == [{"job_id": str(job.id), "provenance": record}]


def test_the_dictionary_version_is_the_installed_package():
    from transcribe_api.domain.provenance_dictation import british_spelling_step

    step = british_spelling_step()
    assert (step.package, step.version) == ("uwotm8", metadata.version("uwotm8"))


def test_provenance_has_no_place_for_content():
    with pytest.raises(ValueError, match="Extra inputs"):
        Provenance(output="transcript", transcript="text")  # type: ignore[call-arg]
