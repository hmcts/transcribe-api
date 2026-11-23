"""DIAAT-408: reading a recording's length from its header with ffprobe (stt/preprocessing.py).

Real ffprobe on real small files made with ffmpeg: the point is that the formats the upload accepts
(and the awkward MP3 with no Xing header) are read within seconds, and that what is not audio is
refused rather than guessed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from tests.unit.recording.unit.async_upload_helpers import tone
from transcribe_api.stt import preprocessing
from transcribe_api.stt.preprocessing import (
    AudioDecodeError,
    ProbedDuration,
    normalize_to_wav,
    parse_probe,
    probe_audio_duration,
    wav_duration_seconds,
)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


@pytest.mark.parametrize("extension", [".wav", ".mp3", ".m4a", ".flac", ".ogg"])
async def test_each_accepted_format_is_probed_to_within_a_fraction_of_a_second(extension, tmp_path):
    path = tmp_path / f"in{extension}"
    path.write_bytes(tone(extension, 3.0))
    probed = await probe_audio_duration(path)
    assert probed.seconds == pytest.approx(3.0, abs=0.2)


@pytest.mark.parametrize("extension", [".wav", ".m4a", ".flac", ".ogg"])
async def test_the_header_of_these_formats_states_the_length_exactly(extension, tmp_path):
    path = tmp_path / f"in{extension}"
    path.write_bytes(tone(extension, 3.0))
    assert (await probe_audio_duration(path)).exact is True


async def test_an_mp3_without_an_xing_header_is_an_estimate_within_a_few_percent(tmp_path):
    # No Xing/Info header: the figure is an estimate from the file size over the bitrate, and says so.
    path = tmp_path / "in.mp3"
    path.write_bytes(tone(".mp3", 20.0, extra=("-write_xing", "0")))
    probed = await probe_audio_duration(path)
    assert probed.exact is False
    assert probed.seconds == pytest.approx(20.0, rel=0.15)


async def test_a_vbr_mp3_without_an_xing_header_is_an_estimate_too(tmp_path):
    path = tmp_path / "in.mp3"
    _ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:duration=20:sample_rate=8000",
        "-ac", "1", "-c:a", "libmp3lame", "-q:a", "4", "-write_xing", "0", str(path),
    )  # fmt: skip
    probed = await probe_audio_duration(path)
    assert probed.exact is False
    assert probed.seconds == pytest.approx(20.0, rel=0.25)  # VBR: the estimate is further out than for CBR


async def test_a_wav_probe_agrees_with_the_wav_duration_the_worker_checks(tmp_path):
    path = tmp_path / "in.wav"
    path.write_bytes(tone(".wav", 2.0))
    assert (await probe_audio_duration(path)).seconds == pytest.approx(wav_duration_seconds(path), abs=0.01)


async def test_the_audio_of_a_video_is_measured_not_the_video(tmp_path):
    """The container says 20 s (the video track); the audio, which is all that is converted, is 6 s."""
    path = tmp_path / "in.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5:d=20",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6:sample_rate=8000",
        "-c:v", "mpeg4", "-c:a", "aac", str(path),
    )  # fmt: skip
    probed = await probe_audio_duration(path)
    assert probed.seconds == pytest.approx(6.0, abs=0.3)
    wav = tmp_path / "out.wav"
    await normalize_to_wav(path, wav)
    assert wav_duration_seconds(wav) == pytest.approx(probed.seconds, abs=0.3)  # what Azure sees


async def test_the_conversion_takes_the_first_audio_stream_the_probe_measures(tmp_path):
    """Two audio streams of different lengths (the second has more channels and, in ffmpeg 8.0.1, the first is
    the default): the probe and the conversion must agree on which one is the recording."""
    path = tmp_path / "two.mkv"
    _ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10:sample_rate=8000",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=4:sample_rate=8000",
        "-map", "0:a", "-map", "1:a", "-c:a", "flac", "-ac:a:1", "2", str(path),
    )  # fmt: skip
    wav = tmp_path / "out.wav"
    await normalize_to_wav(path, wav)
    assert wav_duration_seconds(wav) == pytest.approx(10.0, abs=0.3)  # the first stream, not the stereo one
    probed = await probe_audio_duration(path)
    assert probed is None or probed.seconds == pytest.approx(10.0, abs=0.3)


def _json(*, fmt="mp3", container=100.0, streams=()):
    return json.dumps(
        {
            "streams": list(streams),
            "format": {"format_name": fmt, **({} if container is None else {"duration": str(container)})},
        }
    )


def _audio(duration="100.0", start="0.000000"):
    return {
        "index": 1,
        "codec_type": "audio",
        **({} if duration is None else {"duration": duration}),
        "start_time": start,
    }


@pytest.mark.parametrize(
    ("fmt", "start", "exact"),
    [
        ("mp3", "0.000000", False),  # no Xing/LAME header: the stream starts at 0
        ("mp3", "0.138125", True),  # the header's encoder delay: the length is its frame count
        ("aac", "0.000000", False),  # raw ADTS: size over bitrate
        ("wav", "0.000000", True),
        ("flac", "0.000000", True),
        ("ogg", "0.000000", True),
        ("mov,mp4,m4a,3gp,3g2,mj2", "0.000000", True),
        ("matroska,webm", "0.000000", True),
        ("some_format_not_known_to_be_exact", "0.000000", False),
    ],
)
def test_a_figure_is_exact_only_for_the_formats_that_state_the_length(fmt, start, exact):
    assert parse_probe(_json(fmt=fmt, streams=[_audio(start=start)])) == ProbedDuration(100.0, exact)


def test_the_first_audio_stream_is_the_one_measured_whatever_comes_before_and_after_it():
    streams = [
        {"index": 0, "codec_type": "video", "duration": "999.0"},
        _audio("30.0"),
        {**_audio("50.0"), "index": 2},
    ]
    assert parse_probe(_json(fmt="mov,mp4,m4a,3gp,3g2,mj2", container=999.0, streams=streams)).seconds == 30.0


def test_an_audio_stream_with_no_duration_beside_a_video_is_unknown_not_the_containers():
    streams = [{"index": 0, "codec_type": "video", "duration": "999.0"}, _audio(None)]
    assert parse_probe(_json(fmt="matroska,webm", container=999.0, streams=streams)) is None


def test_an_audio_only_file_with_no_stream_duration_takes_the_containers():
    assert parse_probe(_json(fmt="matroska,webm", container=42.5, streams=[_audio(None)])) == ProbedDuration(42.5, True)
    assert parse_probe(_json(fmt="matroska,webm", container=None, streams=[_audio(None)])) is None


@pytest.mark.parametrize("output", ["", "not json", "[]", '{"streams": [], "format": {"duration": "5"}}'])
def test_no_audio_stream_or_no_json_is_unknown(output):
    assert parse_probe(output) is None


@pytest.mark.parametrize("duration", ["N/A", "nan", "inf", "-3", "abc"])
def test_a_nonsense_stream_duration_is_unknown(duration):
    assert parse_probe(_json(fmt="wav", container=None, streams=[_audio(duration)])) is None


@pytest.mark.parametrize("body", [b"not audio at all, just text " * 40, b"\x00" * 2048, b""])
async def test_what_is_not_audio_is_refused_as_undecodable(body, tmp_path):
    path = tmp_path / "bad.mp3"
    path.write_bytes(body)
    with pytest.raises(AudioDecodeError):
        await probe_audio_duration(path)


async def test_a_missing_ffprobe_is_a_server_problem_not_a_bad_file(tmp_path, monkeypatch):
    async def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "ffprobe")

    monkeypatch.setattr(preprocessing.asyncio, "create_subprocess_exec", missing)
    with pytest.raises(FileNotFoundError):
        await probe_audio_duration(tmp_path / "x.mp3")


async def test_an_ffprobe_that_does_not_answer_in_time_means_the_length_is_unknown(tmp_path, monkeypatch):
    real = asyncio.create_subprocess_exec

    async def slow(*args, **kwargs):
        return await real("sleep", "30", **kwargs)

    monkeypatch.setattr(preprocessing.asyncio, "create_subprocess_exec", slow)
    started = asyncio.get_running_loop().time()
    assert await probe_audio_duration(tmp_path / "x.mp3", timeout=0.2) is None
    assert asyncio.get_running_loop().time() - started < 5  # killed, not waited for
