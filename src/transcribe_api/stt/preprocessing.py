"""FFmpeg audio preprocessing pipeline for transcription quality improvement.

Applies: stereo-to-mono, highpass filter, lowpass filter, volume boost,
dynamic range compression, and loudness normalisation.
"""

from __future__ import annotations

import json
import logging
import math
import wave
from dataclasses import dataclass
from pathlib import Path

from transcribe_api.stt.subprocesses import run_process

logger = logging.getLogger(__name__)


class AudioDecodeError(Exception):
    """Raised when ffmpeg cannot decode the input (corrupt/unsupported format)."""


_FILTER_CHAIN = (
    "pan=mono|c0=0.5*c0+0.5*c1,"
    "highpass=f=100,"
    "lowpass=f=8000,"
    "volume=3.0,"
    "acompressor=threshold=0.1:ratio=4:attack=5:release=50,"
    "loudnorm=I=-16:TP=-1.5:LRA=11"
)

_SAMPLE_RATE = 16000


async def preprocess_audio(input_path: Path, output_path: Path) -> None:
    """Enhance audio for transcription accuracy.

    Raises RuntimeError if ffmpeg is not installed or the conversion fails.
    """
    cmd = [
        "ffmpeg",
        "-i",
        str(input_path),
        "-af",
        _FILTER_CHAIN,
        "-ar",
        str(_SAMPLE_RATE),
        "-ac",
        "1",
        "-y",
        str(output_path),
    ]

    result = await run_process(*cmd)

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg preprocessing failed (exit {result.returncode}): {result.stderr.decode()[:500]}")

    logger.info("Preprocessed audio: %s → %s", input_path.name, output_path.name)


async def normalize_to_wav(input_path: Path, output_path: Path) -> None:
    """Transcode ``input_path`` to a clean 16 kHz mono 16-bit PCM WAV at ``output_path``.

    Azure Speech rejects odd or corrupt encodings with a cryptic error, so every
    accepted upload is normalised to the format it handles most reliably before
    being stored. This is a straight container/codec conversion, with no
    enhancement filters.

    File to file, so the caller never holds the audio in memory: the WAV is
    about 115 MB per hour of audio, several times the size of a typical MP3.
    ``input_path`` should keep the original extension so ffmpeg can sniff the
    source format.

    Raises:
        AudioDecodeError: if ffmpeg exits non-zero (input is corrupt or in an
            unsupported format).
        FileNotFoundError: if the ffmpeg binary is not installed. This is a
            server misconfiguration, not a bad upload, so it is left for the
            caller to report as such.
    """
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-i",
        str(input_path),
        "-map",
        "0:a:0",  # the first audio stream, whatever else the file holds: the one probe_audio_duration measures
        "-ac",
        "1",
        "-ar",
        str(_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "-y",
        str(output_path),
    ]

    result = await run_process(*cmd)

    if result.returncode != 0:
        raise AudioDecodeError(
            f"FFmpeg could not decode the audio (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace')[-500:]}"
        )


def wav_duration_seconds(path: Path) -> float | None:
    """Duration of a PCM WAV file, or None if it cannot be read."""
    try:
        with wave.open(str(path), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
    except Exception:  # a malformed header must never fail the upload
        return None
    return round(frames / rate, 3) if rate > 0 else None


PROBE_TIMEOUT_SECONDS = 10.0

# Container formats (ffprobe's ``format_name``, a comma-separated list of aliases) whose header states the
# length of the audio, so the figure is exact to a fraction of a second. MP3 is only exact when it carries
# a Xing/Info/LAME header (see ``_is_exact``); a raw ADTS ``aac`` stream and anything else not listed has
# its length estimated from the file size over the bitrate.
_EXACT_LENGTH_FORMATS = frozenset({"wav", "w64", "flac", "ogg", "mov", "mp4", "m4a", "matroska", "webm"})


@dataclass(frozen=True)
class ProbedDuration:
    """The length ffprobe read from a file's header, and whether it can be relied on to the second.
    ``exact`` is False for an estimate (an MP3 with no Xing/LAME header: file size over bitrate, off by
    several percent on a VBR file); the exact length is known only once the file is converted."""

    seconds: float
    exact: bool


def _seconds(value: object) -> float | None:
    """A duration ffprobe printed (a string), or None when it printed none ("N/A") or nonsense."""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _is_exact(format_name: str, audio_stream: dict) -> bool:
    names = set(format_name.split(","))
    if "mp3" in names:
        # With a Xing/Info/LAME header ffprobe takes the length from the frame count and reports the encoder
        # delay as the stream's start time (0.138 s for a 8 kHz file); without one the stream starts at 0
        # and the length is an estimate. (A Xing header with no delay information counts as an estimate
        # too: refusing less is the safe side, the exact check after conversion decides.)
        start = _seconds(audio_stream.get("start_time"))
        return start is not None and start > 0
    return bool(names & _EXACT_LENGTH_FORMATS)


def parse_probe(output: str) -> ProbedDuration | None:
    """The length of the audio the conversion will use, from the JSON ffprobe printed.

    ``normalize_to_wav`` maps the first audio stream (``-map 0:a:0``), so that stream's duration is the
    one that counts: a video track longer than its audio (MP4, MKV) is not the recording's length. A
    stream that states no duration (Matroska does not) falls back to the container's, but only when the
    file has no video stream; with a video stream the length is unknown (None: the exact check after the
    conversion decides). A file ffprobe could read that has no audio stream at all raises AudioDecodeError
    (a video-only MP4 is not a recording); None is only for audio whose length is not stated."""
    try:
        data = json.loads(output)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        if isinstance(data.get("streams"), list):
            # ffprobe read the file and it holds no audio (a video-only MP4): not "audio of unknown length",
            # which is None below. The conversion would refuse it, so refuse it before it is stored.
            raise AudioDecodeError("the file has no audio stream")
        return None
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    seconds = _seconds(audio.get("duration"))
    if seconds is None:
        if any(s.get("codec_type") == "video" for s in streams):
            return None
        seconds = _seconds(fmt.get("duration"))
        if seconds is None:
            return None
    return ProbedDuration(seconds, _is_exact(str(fmt.get("format_name") or ""), audio))


async def probe_audio_duration(path: Path, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> ProbedDuration | None:
    """The length of the audio in the file's header, from ``ffprobe`` (a read of the header, not a decode).

    Returns None when the file has an audio stream but states no usable length (some VBR MP3s, raw
    streams, a video with an audio track that states none), or when ffprobe takes longer than ``timeout``: the exact
    length is then known only once the file is converted. See ``ProbedDuration`` for an estimate.

    Raises:
        AudioDecodeError: if ffprobe cannot read the file at all (not audio, or corrupt), or reads it and
            finds no audio stream (a video without sound).
        FileNotFoundError: if ffprobe is not installed (a server misconfiguration, as for ffmpeg).
    """
    try:
        result = await run_process(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration:stream=index,codec_type,duration,start_time",
            "-of",
            "json",
            str(path),
            timeout=timeout,
        )
    except TimeoutError:  # run_process has killed ffprobe
        logger.warning("ffprobe did not answer within %.0f s: the length is checked after conversion", timeout)
        return None
    if result.returncode != 0:
        raise AudioDecodeError(
            f"ffprobe could not read the audio (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace')[-300:]}"
        )
    return parse_probe(result.stdout.decode(errors="replace"))


# Uploads are transcoded with ffmpeg (above); the worker probes and converts
# audio with ffprobe and ffmpeg (stt/utils.py). Both come from the image.
FFMPEG_TOOLS = ("ffmpeg", "ffprobe")


async def missing_ffmpeg_tools() -> list[str]:
    """Return the FFMPEG_TOOLS that are not installed or do not run, in order."""
    missing = []
    for tool in FFMPEG_TOOLS:
        try:
            result = await run_process(tool, "-version")
        except OSError:
            missing.append(tool)
        else:
            if result.returncode != 0:
                missing.append(tool)
    return missing


async def is_ffmpeg_available() -> bool:
    """Return True if ffmpeg and ffprobe are installed and callable."""
    return not await missing_ffmpeg_tools()
