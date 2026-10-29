"""hmcts.area (decision 1): every signal says which area it belongs to.

Each area's routers carry hmcts-otel's area_dependency, and each poller loop runs in
its area; the browser relay and the health checks are shared, with no area.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from hmcts_otel import conventions as c
from hmcts_otel import current_area, record_event
from hmcts_otel.testing import capture_telemetry

from transcribe_api.runtime.telemetry import AREA_DICTATION, AREA_RECORDING


PROBE = "/zz-area-probe"


@pytest.fixture
def tel():
    from transcribe_api.runtime.telemetry import configure_telemetry

    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


@pytest.fixture
def app_with_probes():
    """The real app, with a probe route on each area's router reporting its area."""
    from transcribe_api.api import routes_dictation, routes_dictation_privileged, routes_recording
    from transcribe_api.api.app import create_app

    async def probe() -> dict[str, str]:
        record_event("probe.called")
        return {"area": str(current_area())}

    routers = (routes_dictation.router, routes_dictation_privileged.router, routes_recording.router)
    added = []
    for router in routers:
        router.add_api_route(PROBE, probe, methods=["GET"])
        added.append((router, router.routes[-1]))
    try:
        yield create_app()
    finally:
        for router, route in added:  # exactly the probes: the routers are shared
            router.routes.remove(route)


def test_each_areas_requests_carry_its_area(tel, app_with_probes):
    client = TestClient(app_with_probes)
    assert client.get(f"/api{PROBE}").json() == {"area": AREA_DICTATION}
    assert client.get(f"/api/admin{PROBE}").json() == {"area": AREA_DICTATION}
    assert client.get(f"/api/v1{PROBE}").json() == {"area": AREA_RECORDING}
    assert client.get("/health").status_code == 200
    assert [e[c.AREA] for e in tel.events("probe.called")] == [AREA_DICTATION, AREA_DICTATION, AREA_RECORDING]


def test_the_browser_relay_is_shared_and_has_no_area(tel, app_with_probes):
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user
    from transcribe_api.domain.models_dictation import User

    user = User(id=uuid.uuid4(), email="judge@justice.gov.uk", azure_user_id=str(uuid.uuid4()))
    user.__dict__["app_roles"] = ["Judge"]
    app_with_probes.dependency_overrides[get_current_user] = lambda: user
    response = TestClient(app_with_probes).post("/api/telemetry", json={"events": [{"name": "dictation.paused"}]})

    assert response.json() == {"accepted": 1, "rejected": 0}
    (event,) = tel.events("dictation.paused")
    assert c.AREA not in event


def test_the_relay_still_requires_sign_in(tel, app_with_probes):
    response = TestClient(app_with_probes).post("/api/telemetry", json={"events": [{"name": "dictation.paused"}]})
    assert response.status_code in (401, 403)
    assert tel.events("dictation.paused") == []


def test_a_poller_runs_in_its_area_and_so_do_the_tasks_it_starts():
    from transcribe_api.api.app import _in_area

    async def poller_loop() -> None:
        record_event("poller.polled")

        async def job() -> None:
            record_event("job.started")

        await asyncio.create_task(job())

    with capture_telemetry() as tel:
        asyncio.run(_in_area(AREA_RECORDING, poller_loop))
        record_event("outside.poller")

    assert tel.events("poller.polled")[0][c.AREA] == AREA_RECORDING
    assert tel.events("job.started")[0][c.AREA] == AREA_RECORDING
    assert c.AREA not in tel.events("outside.poller")[0]


def test_each_poller_is_built_and_runs_in_its_own_area(monkeypatch):
    from transcribe_api.api import app as app_module

    seen: dict[str, tuple[str | None, str | None]] = {}

    def fake_poller(label: str):
        class Poller:
            def __init__(self) -> None:
                self.built_in = current_area()

            async def run_polling_loop(self) -> None:
                seen[label] = (self.built_in, current_area())

        return Poller

    monkeypatch.setattr("transcribe_api.stt.polling_service.BatchPollingService", fake_poller("batch"))
    monkeypatch.setattr("transcribe_api.stt.work_poller.TranscriptionPollingService", fake_poller("transcription"))
    monkeypatch.setattr("transcribe_api.stt.job_telemetry.register_job_gauges", lambda: None)

    async def run() -> None:
        await asyncio.gather(*await app_module.start_background_workers())

    asyncio.run(run())
    assert seen == {
        "batch": (AREA_RECORDING, AREA_RECORDING),
        "transcription": (AREA_DICTATION, AREA_DICTATION),
    }


def test_a_malformed_body_is_in_its_area_although_no_route_code_runs(tel, monkeypatch):
    """FastAPI rejects a malformed body (422) before any dependency or route code:
    the area comes from the middleware, by path (PR review)."""
    from transcribe_api.api import app as app_module

    seen: list[str | None] = []
    original = app_module._validation_exception_handler

    async def recording_handler(request, exc):
        seen.append(current_area())
        return await original(request, exc)

    monkeypatch.setattr(app_module, "_validation_exception_handler", recording_handler)
    client = TestClient(app_module.create_app())

    response = client.post(
        "/api/v1/jobs", content=b'{"audio_url": "truncated', headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    assert seen == [AREA_RECORDING]


def test_every_route_maps_to_its_routers_area():
    from transcribe_api.api import routes_dictation, routes_dictation_privileged, routes_recording
    from transcribe_api.api.app import area_for_path

    mounted = [
        *[(route.path, AREA_RECORDING) for route in routes_recording.router.routes],
        *[(f"/api{route.path}", AREA_DICTATION) for route in routes_dictation.router.routes],
        *[(f"/api{route.path}", AREA_DICTATION) for route in routes_dictation_privileged.router.routes],
        *[(f"/api{route.path}", None) for route in routes_dictation.telemetry_router.routes],
        ("/health", None),
        ("/api/openapi.json", None),
    ]
    wrong = [(path, area_for_path(path), expected) for path, expected in mounted if area_for_path(path) != expected]
    assert wrong == []
    assert len(mounted) > 40


def test_an_unhandled_500s_log_is_in_its_area(tel):
    """Starlette runs the 500 handler outside every middleware, AreaMiddleware's area
    included: the handler sets it again (PR review)."""
    from transcribe_api.api import routes_recording
    from transcribe_api.api.app import create_app

    async def boom() -> None:
        raise RuntimeError("unexpected")

    routes_recording.router.add_api_route("/zz-area-boom", boom, methods=["GET"])
    route = routes_recording.router.routes[-1]
    try:
        response = TestClient(create_app(), raise_server_exceptions=False).get("/api/v1/zz-area-boom")
    finally:
        routes_recording.router.routes.remove(route)

    assert response.status_code == 500
    (record,) = [r for r in tel.log_records() if r.body == "Unhandled exception (RuntimeError)"]
    assert dict(record.attributes or {})[c.AREA] == AREA_RECORDING
