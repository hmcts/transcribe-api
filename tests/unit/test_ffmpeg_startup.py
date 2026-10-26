"""Startup check that ffmpeg and ffprobe are installed.

Uploads are transcoded with ffmpeg and the worker probes audio with ffprobe.
Without the binaries every upload failed with an unhandled FileNotFoundError
(a 500), long after the pod had reported healthy. Deployed processes now
refuse to start instead; local runs and tests are not checked.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from transcribe_api import worker
from transcribe_api.api import app as app_module
from transcribe_api.stt import preprocessing


class _FakeProc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    async def communicate(self):
        return (b"", b"")


def _fake_exec(outcomes: dict[str, object]):
    """create_subprocess_exec stand-in: per binary, a return code or an exception."""

    async def fake_exec(binary, *args, **kwargs):
        outcome = outcomes[binary]
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeProc(outcome)

    return fake_exec


class TestMissingFfmpegTools:
    @pytest.mark.asyncio
    async def test_none_missing_when_both_run(self, monkeypatch):
        monkeypatch.setattr(preprocessing.asyncio, "create_subprocess_exec", _fake_exec({"ffmpeg": 0, "ffprobe": 0}))
        assert await preprocessing.missing_ffmpeg_tools() == []

    @pytest.mark.asyncio
    async def test_reports_a_binary_that_is_not_installed(self, monkeypatch):
        monkeypatch.setattr(
            preprocessing.asyncio,
            "create_subprocess_exec",
            _fake_exec({"ffmpeg": 0, "ffprobe": FileNotFoundError("ffprobe")}),
        )
        assert await preprocessing.missing_ffmpeg_tools() == ["ffprobe"]

    @pytest.mark.asyncio
    async def test_reports_a_binary_that_cannot_run(self, monkeypatch):
        monkeypatch.setattr(
            preprocessing.asyncio,
            "create_subprocess_exec",
            _fake_exec({"ffmpeg": PermissionError("ffmpeg"), "ffprobe": 1}),
        )
        assert await preprocessing.missing_ffmpeg_tools() == ["ffmpeg", "ffprobe"]

    @pytest.mark.asyncio
    async def test_is_ffmpeg_available_needs_both(self, monkeypatch):
        monkeypatch.setattr(
            preprocessing.asyncio,
            "create_subprocess_exec",
            _fake_exec({"ffmpeg": 0, "ffprobe": FileNotFoundError("ffprobe")}),
        )
        assert await preprocessing.is_ffmpeg_available() is False


class TestEnsureFfmpegTools:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("environment", ["local", "test"])
    async def test_not_checked_locally_or_in_tests(self, monkeypatch, environment):
        async def must_not_run():
            raise AssertionError("checked")

        monkeypatch.setattr(app_module, "missing_ffmpeg_tools", must_not_run)
        await app_module.ensure_ffmpeg_tools(environment)

    @pytest.mark.asyncio
    async def test_fails_when_a_tool_is_missing(self, monkeypatch, caplog):
        async def missing():
            return ["ffmpeg", "ffprobe"]

        monkeypatch.setattr(app_module, "missing_ffmpeg_tools", missing)
        with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="ffmpeg, ffprobe"):
            await app_module.ensure_ffmpeg_tools("aat")
        assert any("ffmpeg, ffprobe" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)

    @pytest.mark.asyncio
    async def test_passes_and_logs_when_present(self, monkeypatch, caplog):
        async def missing():
            return []

        monkeypatch.setattr(app_module, "missing_ffmpeg_tools", missing)
        with caplog.at_level(logging.INFO):
            await app_module.ensure_ffmpeg_tools("aat")
        assert any("ffmpeg and ffprobe are available" in r.getMessage() for r in caplog.records)


class TestApiStartup:
    def test_checks_ffmpeg_with_the_configured_environment(self, monkeypatch):
        seen = []

        async def recorder(environment):
            seen.append(environment)

        monkeypatch.setattr(app_module, "ensure_ffmpeg_tools", recorder)
        with TestClient(app_module.create_app()):
            pass
        assert seen == ["test"]

    def test_a_failed_check_stops_startup(self, monkeypatch):
        async def fail(environment):
            raise RuntimeError("ffmpeg missing")

        monkeypatch.setattr(app_module, "ensure_ffmpeg_tools", fail)
        with pytest.raises(RuntimeError, match="ffmpeg missing"), TestClient(app_module.create_app()):
            pass


class TestWorkerStartup:
    def test_a_failed_check_stops_the_worker_before_any_poller_starts(self, monkeypatch):
        seen = []
        started = []

        async def fail(environment):
            seen.append(environment)
            raise RuntimeError("ffmpeg missing")

        async def start():
            started.append(1)
            return []

        monkeypatch.setattr(worker, "ensure_ffmpeg_tools", fail)
        monkeypatch.setattr(worker, "start_background_workers", start)
        with pytest.raises(RuntimeError, match="ffmpeg missing"):
            asyncio.run(worker.main())
        assert seen == ["test"]
        assert started == []
