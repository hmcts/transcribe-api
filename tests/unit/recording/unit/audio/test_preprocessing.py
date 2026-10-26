"""Unit tests for audio normalisation (transcode to 16 kHz mono PCM WAV).

These tests never invoke real ffmpeg: the subprocess boundary is patched so the
behaviour is deterministic.
"""

import pytest

from transcribe_api.stt import preprocessing
from transcribe_api.stt.preprocessing import AudioDecodeError, normalize_to_wav


class TestNormalizeToWav:
    """normalize_to_wav works file to file, so neither the upload nor the
    decoded WAV (up to ~115 MB per hour of audio) is held in memory."""

    @pytest.mark.asyncio
    async def test_runs_ffmpeg_from_input_file_to_output_file(self, monkeypatch, tmp_path):
        seen = []

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def fake_exec(*args, **kwargs):
            seen.append(args)
            return FakeProc()

        monkeypatch.setattr(preprocessing.asyncio, "create_subprocess_exec", fake_exec)

        await normalize_to_wav(tmp_path / "in.mp3", tmp_path / "out.wav")

        (args,) = seen
        assert args[0] == "ffmpeg"
        assert str(tmp_path / "in.mp3") in args
        assert args[-1] == str(tmp_path / "out.wav")

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
            await normalize_to_wav(tmp_path / "in.mp3", tmp_path / "out.wav")

    @pytest.mark.asyncio
    async def test_propagates_file_not_found_when_ffmpeg_is_missing(self, monkeypatch, tmp_path):
        async def fake_exec(*args, **kwargs):
            raise FileNotFoundError("ffmpeg")

        monkeypatch.setattr(preprocessing.asyncio, "create_subprocess_exec", fake_exec)

        with pytest.raises(FileNotFoundError):
            await normalize_to_wav(tmp_path / "in.mp3", tmp_path / "out.wav")
