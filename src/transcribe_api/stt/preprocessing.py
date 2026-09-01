"""FFmpeg audio preprocessing pipeline for transcription quality improvement.

Applies: stereo-to-mono, highpass filter, lowpass filter, volume boost,
dynamic range compression, and loudness normalisation.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioDecodeError(Exception):
    """Raised when ffmpeg cannot decode the input (corrupt/unsupported format)."""


_FILTER_CHAIN = ",".join(
    [
        "pan=mono|c0=0.5*c0+0.5*c1",
        "highpass=f=100",
        "lowpass=f=8000",
        "volume=3.0",
        "acompressor=threshold=0.1:ratio=4:attack=5:release=50",
        "loudnorm=I=-16:TP=-1.5:LRA=11",
    ]
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


async def _run_ffmpeg_to_wav(input_path: Path, output_path: Path) -> None:
    """Transcode ``input_path`` to a clean 16 kHz mono 16-bit PCM WAV at ``output_path``.

    No enhancement filters are applied — this is a straight container/codec
    conversion so Azure Speech receives its most reliable input format.

    Raises:
        AudioDecodeError: if ffmpeg exits non-zero (input is corrupt or in an
            unsupported format).
        FileNotFoundError: if the ffmpeg binary is not installed. This is a
            server misconfiguration and is deliberately allowed to propagate.
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


async def normalize_to_wav(content: bytes, source_suffix: str) -> bytes:
    """Transcode arbitrary audio bytes to 16 kHz mono 16-bit PCM WAV bytes.

    Azure Speech rejects odd or corrupt encodings with a cryptic error, so every
    accepted upload is normalised to the format it handles most reliably before
    being stored. This is a clean transcode only — no audio-enhancement filters.

    Args:
        content: the raw uploaded audio bytes.
        source_suffix: the original file extension (e.g. ``".mp3"``), used to
            name the temp input file so ffmpeg can sniff the source format.

    Returns:
        The transcoded WAV file's bytes.

    Raises:
        AudioDecodeError: if the input cannot be decoded (corrupt/unsupported).
        FileNotFoundError: if ffmpeg is not installed (server misconfiguration).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        input_path = tmp / f"input{source_suffix}"
        output_path = tmp / "output.wav"
        input_path.write_bytes(content)
        await _run_ffmpeg_to_wav(input_path, output_path)
        return output_path.read_bytes()


async def is_ffmpeg_available() -> bool:
    """Return True if ffmpeg is installed and callable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except FileNotFoundError:
        return False
