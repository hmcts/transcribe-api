"""Unit tests for Azure Storage utility helpers."""

from transcribe_api.runtime.blob_recording import _sanitize_for_log


class TestSanitizeForLog:
    def test_passes_through_plain_values(self):
        assert _sanitize_for_log("audio-processing/uploads/file.wav") == (
            "'audio-processing/uploads/file.wav'"
        )

    def test_escapes_newlines_that_would_forge_log_lines(self):
        malicious = "file.wav\n2026-01-01 ERROR fake log line injected"
        sanitized = _sanitize_for_log(malicious)
        assert "\n" not in sanitized
        assert "\\n" in sanitized

    def test_escapes_carriage_returns_and_tabs(self):
        sanitized = _sanitize_for_log("a\r\nb\tc")
        assert "\r" not in sanitized
        assert "\n" not in sanitized
        assert "\t" not in sanitized

    def test_stringifies_non_string_values(self):
        assert _sanitize_for_log(ValueError("boom")) == "'boom'"
