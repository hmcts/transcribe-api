"""FFmpeg audio preprocessing pipeline for transcription quality improvement.

Applies: stereo-to-mono, highpass filter, lowpass filter, volume boost,
dynamic range compression, and loudness normalisation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import wave
from pathlib import Path

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

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"FFmpeg preprocessing failed (exit {proc.returncode}): {stderr.decode()[:500]}"
        )

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

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise AudioDecodeError(
            f"FFmpeg could not decode the audio (exit {proc.returncode}): "
            f"{stderr.decode(errors='replace')[-500:]}"
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


def _parse_probed_duration(output: str) -> float | None:
    """The duration ffprobe printed, or None when it printed none ("N/A") or nonsense."""
    try:
        seconds = float(output.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


async def probe_duration_seconds(path: Path, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> float | None:
    """The duration in the file's header, from ``ffprobe`` (a read of the header, not a decode).

    Returns None when the file is readable but states no length (some VBR MP3s, raw streams), or
    when ffprobe takes longer than ``timeout``: the exact length is then known only once the file
    is converted. For a VBR MP3 with no Xing/Info header the figure is an estimate (file size over
    bitrate), usually within a minute: the exact check on the converted WAV is the backstop.

    Raises:
        AudioDecodeError: if ffprobe cannot read the file at all (not audio, or corrupt).
        FileNotFoundError: if ffprobe is not installed (a server misconfiguration, as for ffmpeg).
    """
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        logger.warning("ffprobe did not answer within %.0f s: the length is checked after conversion", timeout)
        return None
    if proc.returncode != 0:
        raise AudioDecodeError(
            f"ffprobe could not read the audio (exit {proc.returncode}): {stderr.decode(errors='replace')[-300:]}"
        )
    return _parse_probed_duration(stdout.decode(errors="replace"))


# Uploads are transcoded with ffmpeg (above); the worker probes and converts
# audio with ffprobe and ffmpeg (stt/utils.py). Both come from the image.
FFMPEG_TOOLS = ("ffmpeg", "ffprobe")


async def missing_ffmpeg_tools() -> list[str]:
    """Return the FFMPEG_TOOLS that are not installed or do not run, in order."""
    missing = []
    for tool in FFMPEG_TOOLS:
        try:
            proc = await asyncio.create_subprocess_exec(
                tool,
                "-version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except OSError:
            missing.append(tool)
        else:
            if proc.returncode != 0:
                missing.append(tool)
    return missing


async def is_ffmpeg_available() -> bool:
    """Return True if ffmpeg and ffprobe are installed and callable."""
    return not await missing_ffmpeg_tools()
