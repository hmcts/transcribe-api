"""DIAAT-280: observe file upload and validation.

Acceptance criteria covered:
* every started upload ends in a visible completed or failed state;
* the cause of a validation or upload failure is identifiable without the audio;
* multi-file uploads can be related to one hearing-level correlation identifier;
* the original file name (a case reference) never reaches logs or telemetry.
"""

from __future__ import annotations

import io
import logging
import wave
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from hmcts_otel import conventions as c
from hmcts_otel import default_redactor, get_tracer
from hmcts_otel.testing import TelemetryCapture, capture_telemetry
from opentelemetry.trace import SpanKind

from transcribe_api.stt.preprocessing import AudioDecodeError
from transcribe_api.runtime.telemetry import JOURNEY_UPLOAD, configure_telemetry, upload_id_from_blob

USER_ID = "00000000-0000-4000-8000-000000000001"
HEARING_ID = "3F2504E0-4F89-41D3-9A0C-0305E82C3301"  # upper case: normalised on export
ORIGINAL_NAME = "PA_50123_2024 hearing"  # what the user's file is called
OTHER_NAME = "Smith v Home Office"  # a file name no case-reference rule would catch
SAMPLE_RATE = 16_000


def _wav(seconds: float) -> bytes:
    """A real 16 kHz mono 16-bit WAV, as normalize_to_wav returns."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"\x00\x00" * int(seconds * SAMPLE_RATE))
    return buffer.getvalue()


def _writes_wav(wav: bytes):
    """normalize_to_wav as on CNP: it writes the WAV to the path it is given."""

    async def normalise(input_path, wav_path):
        wav_path.write_bytes(wav)

    return normalise


@pytest.fixture(autouse=True)
def _no_rate_limit():
    """These tests upload more often than the route's 50/hour limit allows one client."""
    from transcribe_api.api.routes_recording import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()  # registers the service schema and redaction rules
        yield capture


def _client(mocker, *, normalised: bytes | Exception = b"", stored: bool = True) -> TestClient:
    from transcribe_api.api.app import create_app
    from transcribe_api.domain.models_recording import User
    from transcribe_api.domain.auth.dependencies_recording import get_current_user

    manager = mocker.AsyncMock()
    manager.create_blob_from_file = mocker.AsyncMock(return_value=stored)
    manager.build_blob_url = mocker.Mock(return_value="https://x/y.wav")
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("transcribe_api.api.routes_recording.AsyncAzureBlobManager", return_value=manager)
    normalise = (
        mocker.AsyncMock(side_effect=normalised)
        if isinstance(normalised, Exception)
        else mocker.AsyncMock(side_effect=_writes_wav(normalised or _wav(1.5)))
    )
    mocker.patch("transcribe_api.api.routes_recording.normalize_to_wav", new=normalise)
    app = create_app()
    user = mocker.Mock(spec=User)
    user.id = USER_ID
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient, name: str = "hearing.mp3", body: bytes = b"audio", **headers):
    return client.post("/api/v1/uploads", files={"file": (name, body, "audio/mpeg")}, headers=headers or None)


# Validation failures ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "setup", "code"),
    [
        ("notes.txt", None, "upload.unsupported_format"),
        ("hearing.mp3", "too_large", "upload.too_large"),
        ("hearing.mp3", "undecodable", "upload.undecodable"),
    ],
)
def test_each_validation_failure_has_its_own_event_and_code(tel, mocker, name, setup, code):
    if setup == "too_large":
        mocker.patch("transcribe_api.api.routes_recording._MAX_UPLOAD_BYTES", 4)
    client = _client(mocker, normalised=AudioDecodeError("bad") if setup == "undecodable" else b"")

    response = _post(client, name, body=b"0123456789")

    assert response.status_code in (413, 422)
    (invalid,) = tel.events("upload.validation_failed")
    assert invalid[c.ERROR_CODE] == code
    assert invalid[c.JOURNEY_TYPE] == JOURNEY_UPLOAD
    assert invalid["file.size_bytes"] == 10
    (failed,) = tel.events("upload.failed")
    assert failed[c.OUTCOME] == "failure" and failed[c.ERROR_CODE] == code
    assert failed[c.JOURNEY_ID] == invalid[c.JOURNEY_ID]
    names = tel.event_names()
    assert names.index("upload.validation_failed") < names.index("upload.failed")


def test_storage_failure_is_not_a_validation_failure(tel, mocker):
    response = _post(_client(mocker, stored=False))
    assert response.status_code == 502
    assert tel.events("upload.validation_failed") == []
    (failed,) = tel.events("upload.failed")
    assert failed[c.OUTCOME] == "error" and failed[c.ERROR_CODE] == "storage.write_failed"


def test_oversized_upload_is_a_too_large_validation_failure(tel, mocker):
    mocker.patch("transcribe_api.api.routes_recording._MAX_UPLOAD_BYTES", 4)
    response = _post(_client(mocker), body=b"0123456789")
    assert response.status_code == 413
    (invalid,) = tel.events("upload.validation_failed")
    (failed,) = tel.events("upload.failed")
    assert invalid[c.ERROR_CODE] == failed[c.ERROR_CODE] == "upload.too_large"
    assert failed[c.OUTCOME] == "failure"


def test_validation_failed_carries_the_stages_validated_error_code(tel, mocker):
    # An error code the schema rejects becomes the stage's invalid_error_code; the
    # validation_failed event must report that same code, never the raw value.
    from hmcts_otel import stage

    from transcribe_api.api.routes_recording import _reject

    mocker.patch("hmcts_otel._guard.strict", return_value=False)
    with stage("upload") as upload_stage:
        _reject(upload_stage, "Not A Valid Code!")
    (invalid,) = tel.events("upload.validation_failed")
    (failed,) = tel.events("upload.failed")
    assert upload_stage.error_code == "invalid_error_code"
    assert invalid[c.ERROR_CODE] == failed[c.ERROR_CODE] == "invalid_error_code"
    tel.assert_no_prohibited_content("Not A Valid Code!")


# Metadata and measurements -----------------------------------------------------------------


def test_upload_events_carry_safe_typed_metadata(tel, mocker):
    client = _client(mocker, normalised=_wav(1.5))
    response = _post(client, "hearing.MP4", body=b"x" * 1234, **{"X-Hearing-File-Count": "3"})
    assert response.status_code == 201

    (started,) = tel.events("upload.started")
    (done,) = tel.events("upload.completed")
    for event in (started, done):
        assert event["file.size_bytes"] == 1234 and isinstance(event["file.size_bytes"], int)
        assert event["audio.format"] == "mp4"
        assert event["audio.format_category"] == "video"
        assert event["upload.file_count"] == 3
    # Duration of the normalised WAV, known only once it has been decoded.
    assert done["audio.duration_seconds"] == 1.5
    assert isinstance(done["audio.duration_seconds"], float)
    assert done[c.DURATION_MS] >= 0


def test_audio_formats_are_categorised(tel, mocker):
    client = _client(mocker)
    for name in ("a.wav", "b.mp3", "c.m4a", "d.ogg", "e.flac"):
        assert _post(client, name).status_code == 201
    assert {e["audio.format_category"] for e in tel.events("upload.completed")} == {"audio"}


def test_duration_is_omitted_when_the_audio_cannot_be_read(tel, mocker):
    response = _post(_client(mocker, normalised=b"not a wav"))
    assert response.status_code == 201
    (done,) = tel.events("upload.completed")
    assert "audio.duration_seconds" not in done


def test_upload_duration_and_outcome_are_measured(tel, mocker):
    client = _client(mocker)
    _post(client)
    _post(client, "notes.txt")
    points = [p for p in tel.metric_points(c.METRIC_STAGE_DURATION) if p["attributes"][c.STAGE] == "upload"]
    by_outcome = {p["attributes"][c.OUTCOME]: p["count"] for p in points}
    assert by_outcome == {"success": 1, "failure": 1}


# Hearing correlation -----------------------------------------------------------------------


def test_files_of_one_hearing_share_the_hearing_id_as_parent(tel, mocker):
    client = _client(mocker)
    headers = {"X-Hearing-Id": HEARING_ID, "X-Hearing-File-Count": "2"}
    first = _post(client, "part1.mp3", **headers)
    second = _post(client, "part2.txt", **headers)  # a rejected file of the same hearing

    assert first.status_code == 201 and second.status_code == 422
    expected = str(UUID(HEARING_ID))
    events = tel.events()
    upload_events = [e for e in events if e.get(c.JOURNEY_TYPE) == JOURNEY_UPLOAD]
    assert len(upload_events) == 5  # started+completed, started+validation_failed+failed
    assert {e[c.JOURNEY_PARENT_ID] for e in upload_events} == {expected}
    assert len({e[c.JOURNEY_ID] for e in upload_events}) == 2  # one journey per file
    assert upload_id_from_blob(first.json()["blob_name"]) in {e[c.JOURNEY_ID] for e in events}


@pytest.mark.parametrize("bad", ["PA_50123_2024", "not-a-uuid", "1" * 200, ""])
def test_a_malformed_hearing_id_is_ignored_and_not_exported(tel, mocker, bad):
    response = _post(_client(mocker), **{"X-Hearing-Id": bad, "X-Hearing-File-Count": "x"})
    assert response.status_code == 201
    for event in tel.events("upload.started") + tel.events("upload.completed"):
        assert c.JOURNEY_PARENT_ID not in event
        assert "upload.file_count" not in event  # an invalid count is not recorded
    if bad:
        tel.assert_no_prohibited_content(bad)


@pytest.mark.parametrize(
    ("raw", "count"),
    [("1", 1), ("2", 2), ("50", 50), ("0", None), ("51", None), ("2.0", None), ("abc", None)],
)
def test_file_count_header_is_bounded(tel, mocker, raw, count):
    _post(_client(mocker), **{"X-Hearing-File-Count": raw})
    events = tel.events("upload.started") + tel.events("upload.completed")
    assert len(events) == 2
    for event in events:
        assert event.get("upload.file_count") == count
        if count is None:  # missing, not a made-up 1
            assert "upload.file_count" not in event


def test_file_count_is_omitted_without_the_header(tel, mocker):
    _post(_client(mocker))
    for event in tel.events("upload.started") + tel.events("upload.completed"):
        assert "upload.file_count" not in event


@pytest.mark.parametrize(
    ("raw", "count"),
    [
        ("2", 2),
        ("1", 1),
        ("50", 50),
        ("0", None),
        ("51", None),
        ("2 ", None),
        (" 2", None),
        ("abc", None),
        ("", None),
        (None, None),
    ],
)
def test_hearing_file_count_parsing(raw, count):
    from transcribe_api.api.routes_recording import _hearing_file_count

    assert _hearing_file_count(raw) == count


# Every started upload ends -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kwargs", "status", "terminal"),
    [
        ("hearing.mp3", {}, 201, "upload.completed"),
        ("notes.txt", {}, 422, "upload.failed"),
        ("hearing.mp3", {"normalised": AudioDecodeError("bad")}, 422, "upload.failed"),
        ("hearing.mp3", {"stored": False}, 502, "upload.failed"),
        ("hearing.mp3", {"normalised": RuntimeError("ffmpeg crashed")}, 500, "upload.failed"),
    ],
)
def test_every_started_upload_ends_completed_or_failed(tel, mocker, name, kwargs, status, terminal):
    response = _post(_client(mocker, **kwargs), name)
    assert response.status_code == status
    (started,) = tel.events("upload.started")
    ends = tel.events("upload.completed") + tel.events("upload.failed")
    assert [e[c.CUSTOM_EVENT_NAME] for e in ends] == [terminal]
    assert ends[0][c.JOURNEY_ID] == started[c.JOURNEY_ID]
    if status == 500:
        assert ends[0][c.ERROR_CODE] == "unhandled.RuntimeError"


# File names --------------------------------------------------------------------------------


class _FakeBlobTransport:
    """Answers every Azure Storage request with 201, so the real SDK pipeline runs."""

    def __new__(cls):
        import requests
        from azure.core.pipeline.transport import AsyncHttpTransport
        from azure.core.pipeline.transport import (
            AsyncioRequestsTransportResponse as Response,
        )

        class Transport(AsyncHttpTransport):
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def open(self):
                return None

            async def close(self):
                return None

            async def send(self, request, **kwargs):
                raw = requests.Response()
                raw.status_code = 201
                raw._content = b""
                raw.headers.update({"ETag": "x", "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"})
                return Response(request, raw)

        return Transport()


def _real_storage(mocker):
    """The real AsyncAzureBlobManager and Azure SDK pipeline, against a fake transport.

    Also stands in for the Azure SDK's client span (azure_sdk instrumentation, enabled by
    the Azure Monitor distro in deployed environments): a CLIENT span with the blob URL.
    """
    from azure.storage.blob.aio import BlobServiceClient

    from transcribe_api.runtime.blob_recording import AsyncAzureBlobManager

    mocker.patch("transcribe_api.runtime.blob_recording.DefaultAzureCredential")
    real_create = AsyncAzureBlobManager.create_blob_from_file

    async def create(self, content, blob_name, container_name=None):
        url = self.build_blob_url(blob_name)
        attributes = {"url.full": url, "http.url": url, "http.request.method": "PUT"}
        with get_tracer().start_as_current_span("PUT", kind=SpanKind.CLIENT, attributes=attributes):
            return await real_create(self, content, blob_name, container_name)

    mocker.patch.object(AsyncAzureBlobManager, "create_blob_from_file", create)
    mocker.patch.object(AsyncAzureBlobManager, "close", mocker.AsyncMock())
    mocker.patch.object(
        AsyncAzureBlobManager,
        "_blob_service_client",
        lambda self, **options: BlobServiceClient(self.account_url, transport=_FakeBlobTransport(), **options),
    )


def _assert_no_file_name(tel: TelemetryCapture, *names: str) -> None:
    tel.assert_no_prohibited_content(*names)
    lowered = tel.all_exported_text().lower()
    for name in names:
        assert name.lower() not in lowered


# The usual pattern: the case reference first, then party names. The case-reference
# rule alone would redact only the start and leave the party names.
CASE_FIRST_NAME = "PA_50123_2024 Smith v Home Office"


@pytest.mark.parametrize("original", [ORIGINAL_NAME, OTHER_NAME, CASE_FIRST_NAME])
def test_original_file_name_never_reaches_logs_or_telemetry(tel, mocker, original):
    from transcribe_api.api.app import create_app
    from transcribe_api.domain.models_recording import User
    from transcribe_api.domain.auth.dependencies_recording import get_current_user

    _real_storage(mocker)
    mocker.patch("transcribe_api.api.routes_recording.normalize_to_wav", new=_writes_wav(_wav(0.5)))
    app = create_app()
    user = mocker.Mock(spec=User)
    user.id = USER_ID
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app, raise_server_exceptions=False)
    logging.getLogger("azure").setLevel(logging.INFO)  # as in production (root at INFO)

    ok = client.post("/api/v1/uploads", files={"file": (f"{original}.mp3", b"a", "audio/mpeg")})
    rejected = client.post("/api/v1/uploads", files={"file": (f"{original}.txt", b"a", "text/plain")})

    assert ok.status_code == 201, ok.text
    assert rejected.status_code == 422
    sanitised = original.replace(" ", "_")
    text = tel.all_exported_text()
    # The SDK's client span was exported, with the IDs kept. Its request log ("Request
    # URL: ...") never is on CNP: the HTTP logging policy is on hmcts-otel's export-path
    # filter (0.2.3), so the span is where the blob URL can reach telemetry...
    upload_id = upload_id_from_blob(ok.json()["blob_name"])
    assert "Request URL" not in text and f"{upload_id}-[REDACTED]" in text
    assert any(s.kind is SpanKind.CLIENT for s in tel.finished_spans())
    # ...but neither the original file name nor its sanitised form, nor any part of
    # it after the case reference (e.g. party names).
    _assert_no_file_name(tel, original, sanitised)
    tail = sanitised.split("_", 3)[-1] if sanitised.startswith("PA_") else sanitised
    assert tail.lower() not in text.lower()


def test_upload_file_name_redaction_rule():
    configure_telemetry()
    assert [rule.name for rule in default_redactor.rules].count("upload_file_name") == 1
    upload = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    blob = f"uploads/{USER_ID}/{upload}-Smith_v_Home_Office.wav"
    cases = {
        f"Request URL: 'https://a.blob.core.windows.net/audio/{blob}'": (
            f"Request URL: 'https://a.blob.core.windows.net/audio/uploads/{USER_ID}/{upload}-[REDACTED]'"
        ),
        f"Successfully created blob from bytes: 'audio'/'{blob}'": (
            f"Successfully created blob from bytes: 'audio'/'uploads/{USER_ID}/{upload}-[REDACTED]'"
        ),
        f"https://a/audio/{blob}?sv=1": f"https://a/audio/uploads/{USER_ID}/{upload}-[REDACTED]?",
    }
    case_first = f"uploads/{USER_ID}/{upload}-PA_50123_2024_Smith_v_Home_Office.wav"
    cases[f"Request URL: 'https://a/audio/{case_first}'"] = (
        f"Request URL: 'https://a/audio/uploads/{USER_ID}/{upload}-[REDACTED]'"
    )
    for text, expected in cases.items():
        assert default_redactor.redact(text).startswith(expected)
        assert "Smith" not in default_redactor.redact(text)
    # Percent-encoded blob paths (an SDK-quoted blob name), either case.
    for sep in ("%2F", "%2f"):
        encoded = f"uploads{sep}{USER_ID}{sep}{upload}-PA_50123_2024_Smith_v_Home_Office.wav"
        redacted = default_redactor.redact(f"Request URL: 'https://a/audio/{encoded}'")
        assert redacted == (f"Request URL: 'https://a/audio/uploads{sep}{USER_ID}{sep}{upload}-[REDACTED]'")
    # Text that is not an upload blob name is left alone.
    unrelated = f"job {upload} for user {USER_ID}-ok"
    assert default_redactor.redact(unrelated) == unrelated


def test_a_long_file_name_is_redacted_whole_and_quickly():
    import time

    configure_telemetry()
    upload = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    tail = "Smith_v_Home_Office_TAIL"
    name = "PA_50123_2024_" + "x" * 600 + tail + ".wav"  # sanitised names are not truncated
    text = f"Request URL: 'https://a/audio/uploads/{USER_ID}/{upload}-{name}'"
    started = time.perf_counter()
    redacted = default_redactor.redact(text)
    assert redacted == f"Request URL: 'https://a/audio/uploads/{USER_ID}/{upload}-[REDACTED]'"
    assert "TAIL" not in redacted and "xxx" not in redacted
    # No catastrophic backtracking, even on a long run with no blob-name prefix.
    hostile = ("/" + USER_ID) * 2000 + "-" + "y" * 20_000
    default_redactor.redact(hostile + f"/{USER_ID}/{upload}-" + "z" * 50_000)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("padding", ["", " ", " " * 40])
def test_a_padded_hearing_id_is_normalised_whatever_the_padding(padding):
    from transcribe_api.api.routes_recording import _hearing_id

    assert _hearing_id(f"{padding}{HEARING_ID}{padding}") == str(UUID(HEARING_ID))
