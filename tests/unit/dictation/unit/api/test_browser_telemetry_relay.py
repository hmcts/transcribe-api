"""OBS-4: the browser telemetry relay accepts only the registered dictation events.

Acceptance criteria covered:
* the relay rejects any field not on the allowlist, and no transcript, notes or case
  metadata reaches telemetry;
* events are captured for a user who has declined analytics cookies (all dictation
  events are operational);
* the DIAAT-300 signals are queryable: no transcript captured, Speech cancellation
  with reason and error code, autosave failure distinguishable from an auth failure.
"""

from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry

from transcribe_api.runtime.browser_telemetry import JOURNEY_DICTATION_SESSION
from tests.unit.dictation.unit.api.test_routes import _make_user_with_oid

SESSION = "3f2b8c1a-9d4e-4f6a-8b7c-1d2e3f4a5b6c"
# The live-dictation events (OBS-4), registered in the merged schema: all operational.
DICTATION_EVENTS = (
    "dictation.session_requested",
    "speech.token_fetched",
    "speech.session_started",
    "speech.first_result",
    "speech.session_stopped",
    "speech.canceled",
    "speech.unhealthy",
    "dictation.no_speech_warning",
    "dictation.paused",
    "dictation.resumed",
    "dictation.resume_first_result",
    "live_draft.save_failed",
    "live_draft.autosave_stalled",
    "live_draft.save_recovered",
    "live_draft.restore_finished",
    "dictation.submission_finished",
    "auth.token_refresh_failed",
    "dictation.session_ended",
)
TRANSCRIPT = "The appellant " + "said" + " PA/50123/2024 at the hearing"  # built at runtime


def _journey():
    return {"type": JOURNEY_DICTATION_SESSION, "id": SESSION}


def _post(client, events):
    return client.post("/api/telemetry", json={"events": events})


def _signed_in(as_user):
    user = _make_user_with_oid()  # a fresh user each time: the rate limit is per user
    as_user(user)
    return user


def _browser_events(telemetry):
    return [e for e in telemetry.events() if e.get(c.EVENT_SOURCE) == "browser"]


def test_relay_requires_sign_in(client):
    assert _post(client, [{"name": "dictation.paused"}]).status_code in (401, 403)


def test_dictation_lifecycle_is_one_journey(client, as_user):
    _signed_in(as_user)
    sid = "a" * 32
    events = [
        {"name": "dictation.session_requested", "journey": _journey(), "attributes": {"dictation.session_index": 1}},
        {
            "name": "speech.session_started",
            "journey": _journey(),
            "duration_ms": 850,
            "attributes": {"speech.session_id": sid},
        },
        {
            "name": "speech.first_result",
            "journey": _journey(),
            "duration_ms": 2400,
            "attributes": {"speech.session_id": sid},
        },
    ]
    with capture_telemetry() as telemetry:
        response = _post(client, events)
        assert response.status_code == 202
        assert response.json() == {"accepted": 3, "rejected": 0}
        relayed = _browser_events(telemetry)
        assert [e[c.CUSTOM_EVENT_NAME] for e in relayed] == [e["name"] for e in events]
        assert {e[c.JOURNEY_ID] for e in relayed} == {SESSION}
        assert relayed[1]["speech.session_id"] == sid


def test_unknown_events_and_fields_never_reach_telemetry(client, as_user):
    _signed_in(as_user)
    events = [
        {"name": "speech.transcript", "attributes": {"dictation.segments_captured": 1}},  # not registered
        {
            "name": "speech.canceled",
            "journey": _journey(),
            "attributes": {
                "speech.cancel_reason": "Error",
                "speech.error_code": "ConnectionFailure",
                "speech.error_details": TRANSCRIPT,  # not on this event's allowlist
                "notes": TRANSCRIPT,  # not registered at all
            },
        },
        # Free text in an enumerated field is dropped, not stored.
        {"name": "live_draft.save_failed", "attributes": {"live_draft.failure_kind": TRANSCRIPT}},
    ]
    with capture_telemetry() as telemetry:
        assert _post(client, events).json() == {"accepted": 2, "rejected": 1}
        canceled = telemetry.events("speech.canceled")[0]
        assert canceled["speech.cancel_reason"] == "Error"
        assert canceled["speech.error_code"] == "ConnectionFailure"
        assert "live_draft.failure_kind" not in telemetry.events("live_draft.save_failed")[0]
        telemetry.assert_no_prohibited_content(TRANSCRIPT)


def test_a_malformed_batch_or_journey_is_rejected(client, as_user):
    _signed_in(as_user)
    assert _post(client, [{"name": "dictation.paused", "transcript": TRANSCRIPT}]).status_code == 422
    bad_journey = {"type": JOURNEY_DICTATION_SESSION, "id": "PA/50123/2024"}
    assert _post(client, [{"name": "dictation.paused", "journey": bad_journey}]).json()["rejected"] == 1


def test_diaat_300_signals_are_distinct_and_queryable(client, as_user):
    _signed_in(as_user)
    events = [
        {
            "name": "dictation.session_ended",
            "journey": _journey(),
            "attributes": {
                c.OUTCOME: "failure",
                c.ERROR_CODE: "dictation.no_transcript",
                "dictation.segments_captured": 0,
            },
        },
        {
            "name": "live_draft.save_failed",
            "attributes": {"live_draft.failure_kind": "server", "http.response.status_code": 503},
        },
        {
            "name": "live_draft.save_failed",
            "attributes": {"live_draft.failure_kind": "auth", "http.response.status_code": 401},
        },
    ]
    with capture_telemetry() as telemetry:
        assert _post(client, events).json()["accepted"] == 3
        (ended,) = telemetry.events("dictation.session_ended")
        assert ended[c.ERROR_CODE] == "dictation.no_transcript"
        kinds = [e["live_draft.failure_kind"] for e in telemetry.events("live_draft.save_failed")]
        assert kinds == ["server", "auth"]


def test_all_dictation_events_are_operational_so_declining_cookies_keeps_them(client, as_user):
    from hmcts_otel import EventClass, default_schema

    assert all(default_schema.get_event(name).event_class is EventClass.OPERATIONAL for name in DICTATION_EVENTS)
    _signed_in(as_user)
    with capture_telemetry() as telemetry:
        assert _post(client, [{"name": "dictation.paused"}]).json()["accepted"] == 1
        assert telemetry.events("dictation.paused")


def test_rate_limit_is_per_user(client, as_user):
    _signed_in(as_user)
    statuses = [_post(client, [{"name": "dictation.paused"}]).status_code for _ in range(61)]
    assert statuses[:60] == [202] * 60
    assert statuses[60] == 429
    _signed_in(as_user)  # another user is not affected
    assert _post(client, [{"name": "dictation.paused"}]).status_code == 202


def test_every_string_attribute_of_a_browser_event_is_constrained():
    """No browser event can carry free text: each string attribute is an enum or a pattern."""
    from hmcts_otel import default_schema

    browser_events = [name for name, spec in default_schema.events.items() if spec.browser]
    assert len(browser_events) >= len(DICTATION_EVENTS) + 30  # dictation and product events
    for name in browser_events:
        for attribute in default_schema.get_event(name).attributes:
            spec = default_schema.get_attribute(attribute)
            if spec.type is str:
                assert spec.allowed_values or spec.pattern, f"{name}: {attribute} is free text"


# --- OBS-3: product analytics replace PostHog ---------------------------------------


def _consent(client, value):
    client.cookies.set("judicial-transcribe-cookie-preferences", value)


def test_analytics_events_need_analytics_consent(client, as_user):
    _signed_in(as_user)
    event = {"name": "page.viewed", "attributes": {"page.route": "/transcriptions/:id"}}
    with capture_telemetry() as telemetry:
        assert _post(client, [event]).json() == {"accepted": 0, "rejected": 1}  # no cookie
        _consent(client, '{"analytics":"off"}')
        assert _post(client, [event]).json()["accepted"] == 0
        _consent(client, "%7B%22analytics%22%3A%22on%22%7D")  # URL-encoded, as cookie-manager may write it
        assert _post(client, [event]).json()["accepted"] == 1
        assert telemetry.events("page.viewed")[0]["page.route"] == "/transcriptions/:id"
    client.cookies.clear()


def test_failures_are_operational_and_need_no_consent(client, as_user):
    _signed_in(as_user)
    client.cookies.clear()
    event = {
        "name": "minutes.generation_failed",
        "attributes": {c.OUTCOME: "error", c.ERROR_CODE: "TypeError", "minutes.template": "General"},
    }
    assert _post(client, [event]).json()["accepted"] == 1


def test_analytics_attributes_carry_no_free_text(client, as_user):
    _signed_in(as_user)
    _consent(client, '{"analytics":"on"}')
    events = [
        {"name": "page.viewed", "attributes": {"page.route": "/record?case=" + TRANSCRIPT}},
        {"name": "speakers.sample_renamed", "attributes": {"old_name": TRANSCRIPT}},
        {"name": "upload.file_selected", "attributes": {"file.type": TRANSCRIPT}},
    ]
    with capture_telemetry() as telemetry:
        assert _post(client, events).json()["accepted"] == 3
        telemetry.assert_no_prohibited_content(TRANSCRIPT)
    client.cookies.clear()


def test_browser_events_cannot_join_a_server_side_journey(client, as_user):
    """Journey IDs come from the client: another user's transcription journey is off limits."""
    _signed_in(as_user)
    victim = {"type": "dictation.transcription", "id": "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"}
    forged = {
        "name": "dictation.session_ended",
        "journey": victim,
        "attributes": {c.OUTCOME: "error", c.ERROR_CODE: "autosave.failed"},
    }
    with capture_telemetry() as telemetry:
        assert _post(client, [forged]).json() == {"accepted": 0, "rejected": 1}
        assert not [e for e in telemetry.events() if e.get(c.JOURNEY_ID) == victim["id"]]


def test_browser_events_carry_no_client_claimed_transcription_id():
    from hmcts_otel import default_schema

    for name, spec in default_schema.events.items():
        if spec.browser:
            assert "dictation.transcription_id" not in spec.attributes, name


def test_a_browser_supplied_journey_parent_is_dropped(client, as_user):
    """The browser's session is a root journey: a parent_id it sends would let a user link
    their events under any journey, so it never becomes hmcts.journey.parent_id."""
    _signed_in(as_user)
    forged_parent = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    event = {"name": "dictation.paused", "journey": {**_journey(), "parent_id": forged_parent}}
    with capture_telemetry() as telemetry:
        assert _post(client, [event]).json() == {"accepted": 1, "rejected": 0}
        relayed = _browser_events(telemetry)
        assert [e[c.JOURNEY_ID] for e in relayed] == [SESSION]
        assert not [e for e in telemetry.events() if c.JOURNEY_PARENT_ID in e]
        assert forged_parent not in str(telemetry.events())


def test_the_relay_body_limit_still_applies_without_a_declared_length(client, as_user):
    """A body with no declared length is still bounded (hmcts-otel's relay limit, which
    also drops browser-sent journey parents itself)."""
    _signed_in(as_user)

    def chunks():
        yield b'{"events": ['
        yield b" " * 40_000
        yield b"]}"

    response = client.post("/api/telemetry", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_speech_connected_time_is_metered_only_for_accepted_events(client, as_user):
    """OBS-5: real-time Speech usage (live dictation) is the browser-reported Speech
    session length, summed from the unsampled hmcts.event.duration metric by the AI
    usage queries (ai_audio_hours_daily.kql). Only events the relay accepted count."""
    _signed_in(as_user)
    stopped = {"name": "speech.session_stopped", "attributes": {"dictation.segments_captured": 3}}
    events = [
        {**stopped, "journey": _journey(), "duration_ms": 600_000},
        {**stopped, "journey": _journey(), "duration_ms": 1_200_000},
        # Another journey type is refused: not usage.
        {**stopped, "journey": {"type": "dictation.transcription", "id": SESSION}, "duration_ms": 3_600_000},
    ]
    with capture_telemetry() as telemetry:
        assert _post(client, events).json() == {"accepted": 2, "rejected": 1}
        points = [
            p
            for p in telemetry.metric_points(c.METRIC_EVENT_DURATION)
            if p["attributes"].get("event") == "speech.session_stopped"
        ]
        ((point,),) = [points]
        assert point["attributes"]["source"] == "browser"
        assert point["count"] == 2
        assert point["sum"] == 1800.0  # seconds


def _post_raw(client, body: bytes):
    return client.post("/api/telemetry", content=body, headers={"content-type": "application/json"})


def test_a_lone_surrogate_in_a_batch_with_a_parent_is_a_client_error(client, as_user):
    """Removing the parent re-encodes the batch: a lone surrogate (valid JSON, not valid
    UTF-8) must not turn into a 500 that counts toward the server-error alerts."""
    _signed_in(as_user)
    journey = f'{{"type":"{JOURNEY_DICTATION_SESSION}","id":"{SESSION}","parent_id":"6f1c2d3e"}}'
    body = f'{{"events":[{{"name":"dictation.paused","journey":{journey},"attributes":{{"x":"\\ud800"}}}}]}}'.encode()
    with capture_telemetry() as telemetry:
        response = _post_raw(client, body)
        assert 400 <= response.status_code < 500, response.status_code
        assert not [e for e in telemetry.events() if c.JOURNEY_PARENT_ID in e]


def test_deeply_nested_json_is_a_client_error(client, as_user):
    """Nesting that fits inside the body limit can still exhaust json.loads' recursion."""
    _signed_in(as_user)
    depth = 16_000
    body = b'{"events":[{"name":"dictation.paused","attributes":' + b"[" * depth + b"]" * depth + b"}]}"
    assert len(body) < 32_768
    response = _post_raw(client, body)
    assert 400 <= response.status_code < 500, response.status_code
