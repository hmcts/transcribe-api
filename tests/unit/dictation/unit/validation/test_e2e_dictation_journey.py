"""DIAAT-287: validate the Phase 1 dictation telemetry end to end (AC2, AC3).

Each scenario drives the real API router, with production's request-ID middleware,
instrumentation and exception handlers, the way the browser does: the browser
relay's batches (``POST /api/telemetry``, the events ``frontend/lib/dictation-session.ts``
emits, in its order and wire format), the Speech token, autosave, the submission
with document generation, and the document download. Only the outside world is
faked: Azure Speech STS, blob storage and the database (an in-memory stand-in).

Every scenario asserts:
* the journey: browser events on the dictation session, the submission's events
  with the session as their parent, so one query by session ID finds the session
  and its submission; the transcription and its audit rows from there;
* the values the Dictation workbook panels and the alert queries would show for
  these events (``panel_values``, mirrored from the KQL and checked against it).
  They are written to the evidence file, as the expected values for the deployed
  check (VALIDATION.md);
* no prohibited content in anything exported (``conftest.py``: the scan, and its
  evidence file).

The submitted case is synthetic, and full of things that must not leak: names, a
case reference, a postcode, an NI number, email addresses, a SAS URL, and a bearer
token on every request.
"""

from __future__ import annotations

import uuid
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from hmcts_otel import conventions as c
from hmcts_otel.fastapi import RequestIdMiddleware, instrument_fastapi

from transcribe_api.runtime.browser_telemetry import JOURNEY_DICTATION_SESSION
from transcribe_api.domain.models_dictation import User
from transcribe_api.runtime.telemetry import JOURNEY_SUBMISSION
from tests.unit.dictation.unit.validation.pii_scan import (
    APPELLANT_EMAIL,
    BEARER_TOKEN,
    NOTE_TEXT,
    PII_APPELLANT,
    PII_EMAIL,
    PII_JUDGE,
    PII_RESPONDENT,
    SAS_URL,
    TRANSCRIPT_TEXT,
)

OID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SPEECH_SESSION_1 = "0f" * 16  # Speech SDK session IDs: 32 hex
SPEECH_SESSION_2 = "1e" * 16
SUBMISSION = {
    "form_data": {
        "location": "Birmingham",
        "hearingDate": "2026-01-15",
        "judgeName": PII_JUDGE,
        "appellantName": PII_APPELLANT,
        "respondent": PII_RESPONDENT,
        "hearingType": "substantive",
        "appealableDecisionDate": "2025-06-01",
        "documentType": "determination",
    },
    "messages": {
        "background": [{"speaker": "Judge", "text": TRANSCRIPT_TEXT, "timestamp": "00:00:00", "timestampMs": 0}],
        "evidence": [{"speaker": "Judge", "text": f"Email {APPELLANT_EMAIL}", "timestamp": "00:01:00"}],
        "facts": [],
    },
    "notes": NOTE_TEXT,
}
DRAFT = {"transcript": SUBMISSION["messages"], "form_data": SUBMISSION["form_data"]}


# The application, as create_app builds it ------------------------------------------------


@pytest.fixture(autouse=True)
def production(setup_test_environment):
    """The app create_app builds, before any capture starts (it configures telemetry):
    its exception handlers are the ones the scenarios use."""
    from transcribe_api.api.app import create_app

    return create_app()


@pytest.fixture
def app(production, telemetry):
    """The API router with the production app's middleware and exception handlers,
    instrumented inside the capture (instrumentation binds to the provider active when
    applied)."""
    from fastapi.exceptions import HTTPException as FastAPIHTTPException

    import transcribe_api.api.routes_dictation

    test_app = FastAPI()
    test_app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    instrument_fastapi(test_app, request_id=False)
    test_app.add_middleware(RequestIdMiddleware)
    test_app.add_exception_handler(FastAPIHTTPException, production.exception_handlers[FastAPIHTTPException])
    test_app.add_exception_handler(Exception, production.exception_handlers[Exception])
    return test_app


@pytest.fixture
def user(app) -> User:
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    judge = User(id=uuid.uuid4(), email=PII_EMAIL, azure_user_id=OID)
    judge.__dict__["app_roles"] = ["Judge"]
    judge.has_completed_onboarding = True
    app.dependency_overrides[get_current_user] = lambda: judge
    yield judge
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def client(app, user):
    # The browser sends its sign-in token on every request: it must never be exported.
    return TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {BEARER_TOKEN}"})


class Services:
    """The outside world: Speech STS, blob storage and the database."""

    def __init__(self, mocker) -> None:
        self.transcriptions: dict[uuid.UUID, Any] = {}
        self.blobs: dict[str, str] = {}
        self.speech_up = True
        self.document_renders = True
        self.draft_store_up = True

        settings = MagicMock(
            DIRECT_SDK_ACCESS=False,
            APP_URL="http://localhost:8000",
            DOWNLOAD_URL_TTL_MINUTES=60,
            AZURE_STORAGE_CONTAINER_NAME="test-container",
            AZURE_SPEECH_ENDPOINT="https://speech.example",
            AZURE_SPEECH_RESOURCE_ID=None,
            AZURE_SPEECH_KEY="speech-key",
        )
        mocker.patch("transcribe_api.api.routes_dictation.get_settings", return_value=settings)
        mocker.patch("transcribe_api.api.routes_dictation.AsyncAzureBlobManager", side_effect=self._blob_manager)
        mocker.patch("transcribe_api.api.routes_dictation.render_hearing_document", side_effect=self._render)
        mocker.patch("transcribe_api.api.routes_dictation.save_transcription", side_effect=self._save_transcription)
        mocker.patch("transcribe_api.api.routes_dictation.save_transcription_job", side_effect=self._save_job)
        mocker.patch("transcribe_api.api.routes_dictation.get_transcription_by_id", side_effect=self._get_transcription)
        mocker.patch("transcribe_api.api.routes_dictation.upsert_live_draft", side_effect=self._upsert_draft)
        mocker.patch(
            "transcribe_api.api.routes_dictation.generate_blob_download_url", new=AsyncMock(return_value=SAS_URL)
        )
        mocker.patch("transcribe_api.api.routes_dictation.httpx.AsyncClient", side_effect=self._http)

    # Storage
    def _blob_manager(self):
        manager = MagicMock()

        async def create_blob_from_file(path, blob_path):
            self.blobs[blob_path] = str(path)
            return True

        manager.create_blob_from_file = AsyncMock(side_effect=create_blob_from_file)
        manager.set_blob_metadata = AsyncMock(return_value=True)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=manager)
        context.__aexit__ = AsyncMock(return_value=False)
        return context

    def _render(self, request, output_dir, user_email):
        if not self.document_renders:
            # Exception text can quote content: it must never be exported.
            message = f"template field missing for {request.form_data.appellant_name}"
            raise RuntimeError(message)
        path = MagicMock(spec=Path)
        path.__str__ = lambda _: "/tmp/hearing-document.docx"  # noqa: S108
        path.exists.return_value = False
        return path

    # Database
    def _save_transcription(self, transcription, user_id):
        transcription.user_id = user_id
        transcription.__dict__["transcription_jobs"] = []
        self.transcriptions[transcription.id] = transcription
        return transcription

    def _save_job(self, job):
        self.transcriptions[job.transcription_id].__dict__["transcription_jobs"].append(job)
        return job

    def _get_transcription(self, transcription_id, user_id, tz=None):
        found = self.transcriptions.get(transcription_id)
        if found is None or found.user_id != user_id:
            raise HTTPException(status_code=404, detail="Transcription not found")
        return found

    def _upsert_draft(self, user_id, transcript, form_data):
        if not self.draft_store_up:
            # A database error that quotes the row, as drivers do.
            message = f"could not write draft: {form_data['appellantName']} {transcript['background'][0]['text']}"
            raise ConnectionError(message)

    # Speech STS and the storage HTTP client (both httpx in api.routes)
    def _http(self):
        http = MagicMock()
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)
        if self.speech_up:
            token = MagicMock(text="speech-access-token")
            http.post = AsyncMock(return_value=token)
        else:
            http.post = AsyncMock(side_effect=ConnectionError(f"STS refused key speech-key for {PII_EMAIL}"))
        document = MagicMock(status_code=200, headers={"content-type": "application/octet-stream"})

        async def chunks(chunk_size):
            yield b"document bytes"

        document.aiter_bytes = chunks
        document.aclose = AsyncMock()
        http.send = AsyncMock(return_value=document)
        http.aclose = AsyncMock()
        return http


@pytest.fixture
def services(mocker) -> Services:
    return Services(mocker)


# The browser ----------------------------------------------------------------------------


class Browser:
    """What lib/dictation-session.ts and lib/telemetry-relay.ts send for one session:
    the relay's wire format ({name, attributes, journey, duration_ms})."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.session = str(uuid.uuid4())
        self.queue: list[dict[str, Any]] = []

    def emit(self, name: str, attributes: dict[str, Any] | None = None, duration_ms: int | None = None) -> None:
        event: dict[str, Any] = {"name": name, "journey": {"type": JOURNEY_DICTATION_SESSION, "id": self.session}}
        if attributes:
            event["attributes"] = attributes
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        self.queue.append(event)

    def flush(self) -> None:
        response = self.client.post("/api/telemetry", json={"events": self.queue})
        assert response.status_code == 202, response.text
        assert response.json() == {"accepted": len(self.queue), "rejected": 0}
        self.queue = []

    # dictation-session.ts methods, same events and attributes
    def request_recording(self, index: int, reconnect: int = 0, *, resume: bool = False) -> None:
        if resume:
            self.emit("dictation.resumed")
        self.emit(
            "dictation.session_requested", {"dictation.session_index": index, "dictation.reconnect_attempt": reconnect}
        )

    def fetch_token(self, duration_ms: int = 120) -> None:
        response = self.client.get("/api/get-speech-token")
        if response.status_code == 200:
            self.emit("speech.token_fetched", {c.OUTCOME: "success"}, duration_ms)
        else:
            outcome = "failure" if response.status_code < 500 else "error"
            code = f"http.{response.status_code}"
            self.emit("speech.token_fetched", {c.OUTCOME: outcome, c.ERROR_CODE: code}, duration_ms)

    def speech_started(self, sid: str, index: int, reconnect: int = 0, ready_ms: int = 850) -> None:
        attributes = {
            "speech.session_id": sid,
            "dictation.session_index": index,
            "dictation.reconnect_attempt": reconnect,
        }
        self.emit("speech.session_started", attributes, ready_ms)

    def first_text(self, sid: str, index: int, first_text_ms: int = 2400, *, after_resume: bool = False) -> None:
        self.emit("speech.first_result", {"speech.session_id": sid, "dictation.session_index": index}, first_text_ms)
        if after_resume:
            self.emit("dictation.resume_first_result", None, first_text_ms)

    def pause(self, sid: str, index: int, segments: int) -> None:
        self.emit("dictation.paused")
        attributes = {
            "speech.session_id": sid,
            "dictation.session_index": index,
            "dictation.segments_captured": segments,
            "dictation.user_initiated": True,
            "speech.stop_reason": "paused",
        }
        self.emit("speech.session_stopped", attributes, 60_000)

    def stop_running(self, sid: str, index: int, segments: int, reason: str, duration_ms: int = 45_000) -> None:
        """The Speech session still running when the dictation ends (sessionEnded, or the
        hook unmounting): stopped just before the end, not a pause (OBS-5)."""
        attributes = {
            "speech.session_id": sid,
            "dictation.session_index": index,
            "dictation.segments_captured": segments,
            "dictation.user_initiated": False,
            "speech.stop_reason": reason,
        }
        self.emit("speech.session_stopped", attributes, duration_ms)

    def canceled(self, index: int, reason: str, error_code: str | None, collapses: int) -> None:
        attributes = {"dictation.session_index": index, "speech.cancel_reason": reason}
        if error_code:  # undefined values are left out by the relay client
            attributes["speech.error_code"] = error_code
        attributes["dictation.collapses_in_window"] = collapses
        self.emit("speech.canceled", attributes)

    def autosave(self, attempt: int) -> int:
        """PUT /api/live-draft (the draft has the case in it), and the browser's report."""
        response = self.client.put("/api/live-draft", json=DRAFT)
        if response.status_code != 204:
            self.emit(
                "live_draft.save_failed",
                {
                    "live_draft.failure_kind": "server",
                    "http.response.status_code": response.status_code,
                    "live_draft.consecutive_failures": attempt,
                    "live_draft.seconds_since_last_save": 30 * attempt,
                    "live_draft.trigger": "interval",
                },
            )
        return response.status_code

    def submit(self) -> Any:
        response = self.client.post(
            "/api/live-transcription/submission", json=SUBMISSION, headers={"X-Dictation-Session-Id": self.session}
        )
        if response.status_code == 200:
            attributes = {c.OUTCOME: "success", "dictation.mode": "mic"}
        else:
            attributes = {
                c.OUTCOME: "failure" if response.status_code < 500 else "error",
                c.ERROR_CODE: f"http.{response.status_code}",
                "dictation.mode": "mic",
                "http.response.status_code": response.status_code,
            }
        self.emit("dictation.submission_finished", attributes, 1500)
        return response

    def end(
        self,
        outcome: str,
        code: str | None,
        reason: str,
        segments: int,
        speech_sessions: int,
        autosave_failures: int = 0,
    ):
        attributes: dict[str, Any] = {c.OUTCOME: outcome}
        if code:
            attributes[c.ERROR_CODE] = code
        attributes |= {
            "dictation.end_reason": reason,
            "dictation.segments_captured": segments,
            "dictation.speech_sessions": speech_sessions,
            "dictation.autosave_failures": autosave_failures,
        }
        self.emit("dictation.session_ended", attributes, 300_000)
        self.flush()


# The dashboards and alerts, for these events ------------------------------------------
#
# What each Dictation workbook panel and Phase 1 alert query would show for the captured
# events: the KQL's filters and groupings, in Python. test_panel_mirror_matches_the_kql
# checks that the names and values used here are the ones in the KQL files.

PANEL_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "session-outcomes": ("dictation_session_outcomes.kql", ('"dictation.session_ended"', '"hmcts.outcome"')),
    "alert:dictation-session-failures": (
        "alerts/dictation_session_failures.kql",
        ('"dictation.session_ended"', 'outcome == "error"', '"hmcts.error.code"'),
    ),
    "speech-cancellations": (
        "dictation_speech_cancellations.kql",
        ('"speech.canceled"', '"speech.unhealthy"', '"speech.cancel_reason"', '"speech.error_code"'),
    ),
    "alert:speech-failures": (
        "alerts/speech_failures.kql",
        ('"speech.unhealthy"', '"speech.canceled"', '("Error", "StartFailed")', '"hmcts.journey.id"'),
    ),
    "autosave-failures": (
        "dictation_autosave_failures.kql",
        ('"live_draft.save_failed"', '"live_draft.autosave_stalled"', '"live_draft.failure_kind"'),
    ),
    "alert:autosave-stalled": (
        "alerts/autosave_stalled.kql",
        ('"dictation.session"', '"live_draft.autosave_stalled"', 'failure_kind != "auth"'),
    ),
    "submissions": (
        "dictation_submissions.kql",
        ('"submission.completed"', '"submission.failed"', '"dictation.submission"', '"dictation.submission_finished"'),
    ),
    "alert:submission-failures": (
        "alerts/submission_failures.kql",
        ('"submission.failed"', '"dictation.submission"', 'outcome == "error"'),
    ),
    "document-outcomes": (
        "document_generation.kql",
        ('"document_generation.completed"', '"document_generation.failed"', '"hmcts.journey.type"'),
    ),
    "alert:document-generation-failures": (
        "alerts/document_generation_failures.kql",
        ('"document_generation.failed"', 'outcome == "error"'),
    ),
    "start-latency": (
        "dictation_latency.kql",
        ('"speech.session_started"', '"speech.first_result"', '"dictation.resume_first_result"', '"hmcts.duration_ms"'),
    ),
}


def _named(events, *names):
    return [e for e in events if e[c.CUSTOM_EVENT_NAME] in names]


def panel_values(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:  # noqa: C901, PLR0912
    """Panel or alert -> {row label: value}, for the panels these events reach."""
    out: dict[str, Counter | dict] = {name: Counter() for name in PANEL_SOURCES}
    for e in _named(events, "dictation.session_ended"):
        out["session-outcomes"][e[c.OUTCOME]] += 1
        if e[c.OUTCOME] == "error":
            out["alert:dictation-session-failures"][e.get(c.ERROR_CODE, "")] += 1
    speech_sessions: dict[str, set] = {}
    for e in _named(events, "speech.canceled", "speech.unhealthy"):
        reason, code = e.get("speech.cancel_reason", ""), e.get("speech.error_code", "")
        out["speech-cancellations"][f"{e[c.CUSTOM_EVENT_NAME]} {reason} {code}".rstrip()] += 1
        if e[c.CUSTOM_EVENT_NAME] == "speech.unhealthy" or reason in ("Error", "StartFailed"):
            speech_sessions.setdefault(code, set()).add(e.get(c.JOURNEY_ID, ""))
    out["alert:speech-failures"] = {f"error_code={k!r} sessions": len(v) for k, v in speech_sessions.items()}
    session_events = [e for e in events if e.get(c.JOURNEY_TYPE) == JOURNEY_DICTATION_SESSION]
    for e in _named(events, "live_draft.save_failed", "live_draft.autosave_stalled", "live_draft.save_recovered"):
        out["autosave-failures"][f"{e[c.CUSTOM_EVENT_NAME]} {e.get('live_draft.failure_kind', '')}".rstrip()] += 1
    if session_events:
        sessions = {e[c.JOURNEY_ID] for e in session_events}
        stalled = {
            e[c.JOURNEY_ID]
            for e in _named(session_events, "live_draft.autosave_stalled")
            if e.get("live_draft.failure_kind") != "auth"
        }
        if stalled:
            out["alert:autosave-stalled"] = {
                "stalled_pct": round(100.0 * len(stalled) / len(sessions), 1),
                "sessions": len(sessions),
            }
    for e in _named(events, "submission.completed", "submission.failed", "submission.cancelled"):
        if e.get(c.JOURNEY_TYPE) == JOURNEY_SUBMISSION:
            out["submissions"][f"server {e[c.OUTCOME]}"] += 1
            if e[c.CUSTOM_EVENT_NAME] == "submission.failed" and e[c.OUTCOME] == "error":
                out["alert:submission-failures"][e[c.ERROR_CODE]] += 1
    for e in _named(events, "dictation.submission_finished"):
        out["submissions"][f"browser {e[c.OUTCOME]}"] += 1
    for e in _named(events, "document_generation.completed", "document_generation.failed"):
        out["document-outcomes"][f"{e.get(c.JOURNEY_TYPE)} {e[c.OUTCOME]}"] += 1
        if e[c.CUSTOM_EVENT_NAME] == "document_generation.failed" and e[c.OUTCOME] == "error":
            out["alert:document-generation-failures"][f"{e.get(c.JOURNEY_TYPE)} {e[c.ERROR_CODE]}"] += 1
    latency = {}
    for e in _named(events, "speech.session_started", "speech.first_result", "dictation.resume_first_result"):
        name = e[c.CUSTOM_EVENT_NAME]
        measure = {"speech.session_started": "start_to_ready", "speech.first_result": "start_to_first_text"}.get(
            name, "resume_to_next_text"
        )
        index = e.get("dictation.session_index")
        latency[f"{measure} session_index={index}" if index else measure] = e[c.DURATION_MS]
    out["start-latency"] = latency
    return {name: dict(values) for name, values in out.items() if values}


def _record(scenario_notes, telemetry, expected):
    """Assert the panel values, and put them in the evidence for the deployed check."""
    values = panel_values(telemetry.events())
    assert values == expected
    scenario_notes["expected_dashboard_values"] = values


# The journey ------------------------------------------------------------------------------


def _session_query(events, session):
    """customEvents | where hmcts.journey.id == session or hmcts.journey.parent_id == session"""
    return [e for e in events if session in (e.get(c.JOURNEY_ID), e.get(c.JOURNEY_PARENT_ID))]


def _start(browser, *, ready_ms=850, first_text_ms=2400):
    browser.request_recording(1)
    browser.fetch_token()
    browser.speech_started(SPEECH_SESSION_1, 1, ready_ms=ready_ms)
    browser.first_text(SPEECH_SESSION_1, 1, first_text_ms)


BROWSER_SUCCESS = [
    "dictation.session_requested",
    "speech.token_fetched",
    "speech.session_started",
    "speech.first_result",
    "dictation.paused",
    "speech.session_stopped",
    "dictation.resumed",
    "dictation.session_requested",
    "speech.token_fetched",
    "speech.session_started",
    "speech.first_result",
    "dictation.resume_first_result",
    "dictation.paused",
    "speech.session_stopped",
    "dictation.submission_finished",
    "dictation.session_ended",
]
SUBMISSION_SUCCESS = [
    "submission.started",
    "document_generation.started",
    "document_generation.completed",
    "document.available",
    "transcript.available",
    "submission.completed",
]


def test_successful_dictation_submission_and_download_is_one_traceable_journey(
    client, services, telemetry, audit_log, scenario_notes
):
    browser = Browser(client)
    _start(browser)
    assert browser.autosave(1) == 204
    browser.pause(SPEECH_SESSION_1, 1, segments=4)
    browser.flush()
    browser.request_recording(2, resume=True)
    browser.fetch_token()
    browser.speech_started(SPEECH_SESSION_2, 2, ready_ms=700)
    browser.first_text(SPEECH_SESSION_2, 2, 1900, after_resume=True)
    browser.pause(SPEECH_SESSION_2, 2, segments=3)
    browser.flush()
    submitted = browser.submit()
    assert submitted.status_code == 200, submitted.text
    browser.end("success", None, "submitted", segments=7, speech_sessions=2)

    # The download: the link (an export, DIAAT-286), then the document itself.
    transcription_id = submitted.json()["transcription_id"]
    link = client.get(f"/api/transcriptions/{transcription_id}/document-download")
    assert link.status_code == 200, link.text
    download = client.get(link.json()["download_url"].removeprefix("http://localhost:8000"))
    assert download.status_code == 200
    assert download.content == b"document bytes"

    events = telemetry.events()
    # One query by the session ID finds the browser session and its submission, in order.
    journey = _session_query(events, browser.session)
    browser_events = [e for e in journey if e.get(c.JOURNEY_TYPE) == JOURNEY_DICTATION_SESSION]
    submission_events = [e for e in journey if e.get(c.JOURNEY_TYPE) == JOURNEY_SUBMISSION]
    assert [e[c.CUSTOM_EVENT_NAME] for e in browser_events] == BROWSER_SUCCESS
    assert {e[c.EVENT_SOURCE] for e in browser_events} == {"browser"}
    assert [e[c.CUSTOM_EVENT_NAME] for e in submission_events] == SUBMISSION_SUCCESS
    assert len(journey) == len(BROWSER_SUCCESS) + len(SUBMISSION_SUCCESS)
    assert {e[c.JOURNEY_PARENT_ID] for e in submission_events} == {browser.session}
    assert len({e[c.JOURNEY_ID] for e in submission_events}) == 1
    # Nothing of the submission is outside the query (no event lost its parent).
    assert not [e for e in _named(events, *SUBMISSION_SUCCESS) if e not in submission_events]

    (available,) = _named(journey, "document.available")
    assert available["document.template"] == "decision"
    (transcript,) = _named(journey, "transcript.available")
    assert transcript["dictation.transcription_id"] == transcription_id
    (stopped, _) = _named(journey, "speech.session_stopped")
    assert stopped["dictation.user_initiated"] is True

    # From the transcription to its audit rows (application database), joined to the
    # requests by request ID.
    # One download, one row: without direct storage access the link only points at
    # /api/download, which records the export when the document is streamed.
    rows = audit_log.rows()
    assert [(r.event_type, r.outcome, r.object_id, r.detail) for r in rows] == [
        ("transcript.export", "success", transcription_id, {"via": "stream"}),
    ]
    assert [r.request_id for r in rows] == [download.headers["x-request-id"]]
    server_spans = {s.attributes.get(c.REQUEST_ID): s for s in telemetry.finished_spans() if s.kind.name == "SERVER"}
    for row in rows:
        assert row.request_id in server_spans  # the requests row of the same request
        assert row.user_id == str(next(iter(services.transcriptions.values())).user_id)
    # Every server event of the submission carries the submission request's ID.
    (submission_request,) = {e[c.REQUEST_ID] for e in submission_events}
    assert submission_request == submitted.headers["x-request-id"]

    _record(
        scenario_notes,
        telemetry,
        {
            "session-outcomes": {"success": 1},
            "submissions": {"server success": 1, "browser success": 1},
            "document-outcomes": {"dictation.submission success": 1},
            "start-latency": {
                "start_to_ready session_index=1": 850,
                "start_to_first_text session_index=1": 2400,
                "start_to_ready session_index=2": 700,
                "start_to_first_text session_index=2": 1900,
                "resume_to_next_text": 1900,
            },
        },
    )


def test_slow_speech_start_shows_in_the_start_latency(client, services, telemetry, scenario_notes):
    browser = Browser(client)
    _start(browser, ready_ms=9_000, first_text_ms=14_500)  # throttled network or a far Speech region
    browser.flush()
    assert browser.submit().status_code == 200
    browser.stop_running(SPEECH_SESSION_1, 1, segments=2, reason="submitted")
    browser.end("success", None, "submitted", segments=2, speech_sessions=1)
    (started,) = telemetry.events("speech.session_started")
    # Submitted while recording: the running Speech session is counted, then the end.
    names = [e[c.CUSTOM_EVENT_NAME] for e in _session_query(telemetry.events(), browser.session)]
    assert names[-3:] == ["dictation.submission_finished", "speech.session_stopped", "dictation.session_ended"]
    assert "dictation.paused" not in names
    (stopped,) = telemetry.events("speech.session_stopped")
    assert (stopped["speech.stop_reason"], stopped[c.DURATION_MS]) == ("submitted", 45_000)
    assert started[c.JOURNEY_ID] == browser.session
    assert started["dictation.reconnect_attempt"] == 0  # a user start: the start-latency panel's "user" series
    _record(
        scenario_notes,
        telemetry,
        {
            "session-outcomes": {"success": 1},
            "submissions": {"server success": 1, "browser success": 1},
            "document-outcomes": {"dictation.submission success": 1},
            "start-latency": {"start_to_ready session_index=1": 9000, "start_to_first_text session_index=1": 14500},
        },
    )


def test_failed_speech_start_with_an_invalid_token(client, services, telemetry, scenario_notes):
    """The token is issued, but Speech refuses it: the SDK cancels with
    AuthenticationFailure, the reconnects fail too, and the session collapses."""
    browser = Browser(client)
    browser.request_recording(1)
    browser.fetch_token()
    browser.canceled(1, "Error", "AuthenticationFailure", collapses=1)
    for attempt in (1, 2):
        browser.request_recording(1 + attempt, reconnect=attempt)
        browser.canceled(1 + attempt, "Error", "AuthenticationFailure", collapses=1 + attempt)
    browser.emit(
        "speech.unhealthy",
        {
            "dictation.collapses_in_window": 3,
            "dictation.window_seconds": 60,
            "speech.cancel_reason": "Error",
            "speech.error_code": "AuthenticationFailure",
        },
    )
    browser.end("failure", "dictation.no_transcript", "left_page", segments=0, speech_sessions=0)

    journey = _session_query(telemetry.events(), browser.session)
    assert [e[c.CUSTOM_EVENT_NAME] for e in journey][-2:] == ["speech.unhealthy", "dictation.session_ended"]
    assert {e["speech.error_code"] for e in _named(journey, "speech.canceled")} == {"AuthenticationFailure"}
    assert not _named(journey, "speech.session_started", "speech.first_result")
    _record(
        scenario_notes,
        telemetry,
        {
            # No transcript is always a failure, not an error: dictation-session-failures
            # (outcome == "error") does not count it. speech-failures does.
            "session-outcomes": {"failure": 1},
            "speech-cancellations": {
                "speech.canceled Error AuthenticationFailure": 3,
                "speech.unhealthy Error AuthenticationFailure": 1,
            },
            "alert:speech-failures": {"error_code='AuthenticationFailure' sessions": 1},
        },
    )


def test_failed_speech_start_when_the_token_service_is_down(client, services, telemetry, scenario_notes):
    """Speech STS unreachable: the server's speech_token stage fails, the browser
    reports the token failure and a start that never happened (StartFailed)."""
    services.speech_up = False
    browser = Browser(client)
    browser.request_recording(1)
    browser.fetch_token(duration_ms=5_000)
    browser.canceled(1, "StartFailed", None, collapses=1)
    browser.end("failure", "dictation.no_transcript", "left_page", segments=0, speech_sessions=0)

    (failed,) = telemetry.events("speech_token.failed")
    assert (failed[c.OUTCOME], failed[c.ERROR_CODE]) == ("error", "speech_token.request_failed")
    (fetched,) = telemetry.events("speech.token_fetched")
    assert (fetched[c.OUTCOME], fetched[c.ERROR_CODE], fetched[c.JOURNEY_ID]) == ("error", "http.500", browser.session)
    # The server's token stage is not on the session's journey (the token request
    # carries no session ID): it is joined by user and time, or the browser's event.
    assert c.JOURNEY_ID not in failed
    _record(
        scenario_notes,
        telemetry,
        {
            "session-outcomes": {"failure": 1},
            "speech-cancellations": {"speech.canceled StartFailed": 1},
            # StartFailed has no SDK error code: the alert's error_code dimension is empty.
            "alert:speech-failures": {"error_code='' sessions": 1},
        },
    )


def test_interrupted_session_with_failing_autosave_then_tab_closed(client, services, telemetry, scenario_notes):
    """The draft store fails (the database error quotes the draft), autosave stalls,
    and the tab is closed: the session ends in error with autosave.failed."""
    browser = Browser(client)
    _start(browser)
    services.draft_store_up = False
    statuses = [browser.autosave(attempt) for attempt in (1, 2, 3)]
    assert statuses == [500, 500, 500]
    browser.emit(
        "live_draft.autosave_stalled",
        {
            "live_draft.failure_kind": "server",
            "live_draft.consecutive_failures": 3,
            "live_draft.seconds_since_last_save": 90,
        },
    )
    # pagehide: the running Speech session's stop and the end are sent with keepalive.
    browser.stop_running(SPEECH_SESSION_1, 1, segments=5, reason="page_exit")
    browser.end("error", "autosave.failed", "page_hidden", segments=5, speech_sessions=1, autosave_failures=3)

    (ended,) = _named(_session_query(telemetry.events(), browser.session), "dictation.session_ended")
    assert (ended[c.OUTCOME], ended[c.ERROR_CODE], ended["dictation.end_reason"]) == (
        "error",
        "autosave.failed",
        "page_hidden",
    )
    # The server side of each failed save: a failed request span with its exception type only.
    failed_puts = [
        s
        for s in telemetry.finished_spans()
        if s.kind.name == "SERVER" and s.attributes.get("http.route") == "/api/live-draft"
    ]
    assert len(failed_puts) == 3
    assert all(s.status.status_code.name == "ERROR" for s in failed_puts)
    _record(
        scenario_notes,
        telemetry,
        {
            "session-outcomes": {"error": 1},
            "alert:dictation-session-failures": {"autosave.failed": 1},
            "autosave-failures": {"live_draft.save_failed server": 3, "live_draft.autosave_stalled server": 1},
            "alert:autosave-stalled": {"stalled_pct": 100.0, "sessions": 1},
            "start-latency": {"start_to_ready session_index=1": 850, "start_to_first_text session_index=1": 2400},
        },
    )


def test_interrupted_session_network_lost_before_any_text(client, services, telemetry, scenario_notes):
    """The Speech connection drops before any text, and the tab is closed."""
    browser = Browser(client)
    browser.request_recording(1)
    browser.fetch_token()
    browser.speech_started(SPEECH_SESSION_1, 1)
    browser.canceled(1, "Error", "ConnectionFailure", collapses=1)
    browser.end("failure", "dictation.no_transcript", "page_hidden", segments=0, speech_sessions=1)

    (ended,) = telemetry.events("dictation.session_ended")
    assert (ended[c.ERROR_CODE], ended["dictation.end_reason"]) == ("dictation.no_transcript", "page_hidden")
    _record(
        scenario_notes,
        telemetry,
        {
            "session-outcomes": {"failure": 1},
            "speech-cancellations": {"speech.canceled Error ConnectionFailure": 1},
            "alert:speech-failures": {"error_code='ConnectionFailure' sessions": 1},
            "start-latency": {"start_to_ready session_index=1": 850},
        },
    )


def test_document_generation_failure_is_on_the_session_and_alerts(client, services, telemetry, scenario_notes):
    """Rendering fails (its exception quotes the case): the submission fails, the user
    gives up, and the failure is traceable from the session."""
    browser = Browser(client)
    _start(browser)
    browser.flush()
    services.document_renders = False
    response = browser.submit()
    assert response.status_code == 500
    browser.stop_running(SPEECH_SESSION_1, 1, segments=3, reason="page_exit")
    browser.end("error", "submission.failed", "left_page", segments=3, speech_sessions=1)

    journey = _session_query(telemetry.events(), browser.session)
    submission = [e[c.CUSTOM_EVENT_NAME] for e in journey if e.get(c.JOURNEY_TYPE) == JOURNEY_SUBMISSION]
    assert submission == [
        "submission.started",
        "document_generation.started",
        "document_generation.failed",
        "submission.failed",
    ]
    (failed,) = _named(journey, "document_generation.failed")
    assert failed[c.ERROR_CODE] == "document.render_failed"
    assert not _named(journey, "document.available", "transcript.available")
    _record(
        scenario_notes,
        telemetry,
        {
            "session-outcomes": {"error": 1},
            "alert:dictation-session-failures": {"submission.failed": 1},
            "submissions": {"server error": 1, "browser error": 1},
            "alert:submission-failures": {"unhandled.RuntimeError": 1},
            "document-outcomes": {"dictation.submission error": 1},
            "alert:document-generation-failures": {"dictation.submission document.render_failed": 1},
            "start-latency": {"start_to_ready session_index=1": 850, "start_to_first_text session_index=1": 2400},
        },
    )
