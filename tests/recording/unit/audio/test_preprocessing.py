"""Unit tests for audio normalisation (transcode to 16 kHz mono PCM WAV).

These tests never invoke real ffmpeg: the subprocess boundary is patched so the
behaviour is deterministic.
"""

from pathlib import Path

import pytest

from transcribe_api.stt import preprocessing
from transcribe_api.stt.preprocessing import (
    AudioDecodeError,
    _run_ffmpeg_to_wav,
    normalize_to_wav,
)


class TestNormalizeToWav:
    @pytest.mark.asyncio
    async def test_returns_transcoded_wav_bytes(self, monkeypatch):
        async def fake_run(input_path: Path, output_path: Path) -> None:
            # Stand in for ffmpeg: write known WAV bytes to the output path.
            output_path.write_bytes(b"RIFF....WAVE")

        monkeypatch.setattr(preprocessing, "_run_ffmpeg_to_wav", fake_run)

        result = await normalize_to_wav(b"input", ".mp3")

        assert result == b"RIFF....WAVE"

    @pytest.mark.asyncio
    async def test_propagates_decode_error(self, monkeypatch):
        async def fake_run(input_path: Path, output_path: Path) -> None:
            raise AudioDecodeError("boom")

        monkeypatch.setattr(preprocessing, "_run_ffmpeg_to_wav", fake_run)

        with pytest.raises(AudioDecodeError):
            await normalize_to_wav(b"input", ".mp3")


class TestRunFfmpegToWav:
    @pytest.mark.asyncio
    async def test_raises_audio_decode_error_on_nonzero_returncode(self, monkeypatch, tmp_path):
        class FakeProc:
            returncode = 1

            async def communicate(self):
                return (b"", b"boom")

        async def fake_exec(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(preprocessing.asyncio, "create_subprocess_exec", fake_exec)

        with pytest.raises(AudioDecodeError, match="boom"):
            await _run_ffmpeg_to_wav(tmp_path / "in.mp3", tmp_path / "out.wav")
