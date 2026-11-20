"""DIAAT-408: reading a recording's length from its header with ffprobe (stt/preprocessing.py).

Real ffprobe on real small files made with ffmpeg: the point is that the formats the upload accepts
(and the awkward MP3 with no Xing header) are read within seconds, and that what is not audio is
refused rather than guessed.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.unit.recording.unit.async_upload_helpers import tone
from transcribe_api.stt import preprocessing
from transcribe_api.stt.preprocessing import (
    AudioDecodeError,
    _parse_probed_duration,
    probe_duration_seconds,
    wav_duration_seconds,
)


@pytest.mark.parametrize("extension", [".wav", ".mp3", ".m4a", ".flac", ".ogg"])
async def test_each_accepted_format_is_probed_to_within_a_fraction_of_a_second(extension, tmp_path):
    path = tmp_path / f"in{extension}"
    path.write_bytes(tone(extension, 3.0))
    assert await probe_duration_seconds(path) == pytest.approx(3.0, abs=0.2)


async def test_an_mp3_without_an_xing_header_is_probed_to_within_a_second(tmp_path):
    # No Xing/Info header: the figure is an estimate from the file size over the bitrate.
    path = tmp_path / "in.mp3"
    path.write_bytes(tone(".mp3", 20.0, extra=("-write_xing", "0")))
    assert await probe_duration_seconds(path) == pytest.approx(20.0, abs=1.0)


async def test_a_wav_probe_agrees_with_the_wav_duration_the_worker_checks(tmp_path):
    path = tmp_path / "in.wav"
    path.write_bytes(tone(".wav", 2.0))
    assert await probe_duration_seconds(path) == pytest.approx(wav_duration_seconds(path), abs=0.01)


@pytest.mark.parametrize("body", [b"not audio at all, just text " * 40, b"\x00" * 2048, b""])
async def test_what_is_not_audio_is_refused_as_undecodable(body, tmp_path):
    path = tmp_path / "bad.mp3"
    path.write_bytes(body)
    with pytest.raises(AudioDecodeError):
        await probe_duration_seconds(path)


async def test_a_missing_ffprobe_is_a_server_problem_not_a_bad_file(tmp_path, monkeypatch):
    async def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "ffprobe")

    monkeypatch.setattr(preprocessing.asyncio, "create_subprocess_exec", missing)
    with pytest.raises(FileNotFoundError):
        await probe_duration_seconds(tmp_path / "x.mp3")


async def test_an_ffprobe_that_does_not_answer_in_time_means_the_length_is_unknown(tmp_path, monkeypatch):
    real = asyncio.create_subprocess_exec

    async def slow(*args, **kwargs):
        return await real("sleep", "30", **kwargs)

    monkeypatch.setattr(preprocessing.asyncio, "create_subprocess_exec", slow)
    started = asyncio.get_running_loop().time()
    assert await probe_duration_seconds(tmp_path / "x.mp3", timeout=0.2) is None
    assert asyncio.get_running_loop().time() - started < 5  # killed, not waited for


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("12.5\n", 12.5),
        ("0.000000\n", 0.0),
        ("N/A\n", None),
        ("", None),
        ("nan\n", None),
        ("inf\n", None),
        ("-3\n", None),
        ("abc", None),
    ],
)
def test_the_probed_duration_is_parsed_defensively(output, expected):
    assert _parse_probed_duration(output) == expected
