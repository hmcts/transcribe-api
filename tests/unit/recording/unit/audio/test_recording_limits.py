"""DIAAT-408: how long a recording may be, and the settings behind it (stt/recording_limits.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from transcribe_api.runtime import settings_dictation
from transcribe_api.runtime.settings_recording import AZURE_BATCH_FILE_LIMIT_SECONDS, Settings
from transcribe_api.stt.recording_limits import current_limit_seconds, effective_limit_seconds

FOUR_HOURS = 4 * 60 * 60
EIGHT_HOURS = 8 * 60 * 60


def test_azures_limit_per_file_is_240_minutes():
    assert AZURE_BATCH_FILE_LIMIT_SECONDS == 240 * 60


@pytest.mark.parametrize(
    ("long_recordings", "async_preparation", "expected"),
    [
        (False, False, FOUR_HOURS),
        (True, False, FOUR_HOURS),  # long recordings need background preparation as well
        (False, True, FOUR_HOURS),
        (True, True, EIGHT_HOURS),
    ],
)
def test_effective_limit_in_all_four_flag_combinations(long_recordings, async_preparation, expected):
    assert (
        effective_limit_seconds(
            long_recordings=long_recordings, async_preparation=async_preparation, max_seconds=EIGHT_HOURS
        )
        == expected
    )


@pytest.mark.parametrize(
    ("long_recordings", "async_preparation"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_a_maximum_below_240_minutes_applies_whatever_the_flags(long_recordings, async_preparation):
    assert (
        effective_limit_seconds(long_recordings=long_recordings, async_preparation=async_preparation, max_seconds=3600)
        == 3600
    )


def test_a_maximum_above_8_hours_applies_with_both_flags_on():
    assert effective_limit_seconds(long_recordings=True, async_preparation=True, max_seconds=12 * 3600) == 12 * 3600


def _flag_defaults(monkeypatch, *, long_recordings: bool, async_preparation: bool) -> None:
    monkeypatch.setenv("LONG_RECORDINGS_ENABLED", str(long_recordings).lower())
    monkeypatch.setenv("ASYNC_UPLOAD_PREPARATION", str(async_preparation).lower())
    settings_dictation.get_settings.cache_clear()


def test_the_flags_are_off_by_default_and_the_limit_is_240_minutes(monkeypatch):
    monkeypatch.delenv("LONG_RECORDINGS_ENABLED", raising=False)
    monkeypatch.delenv("ASYNC_UPLOAD_PREPARATION", raising=False)
    settings_dictation.get_settings.cache_clear()
    assert settings_dictation.get_settings().LONG_RECORDINGS_ENABLED is False
    assert settings_dictation.get_settings().ASYNC_UPLOAD_PREPARATION is False
    assert current_limit_seconds() == FOUR_HOURS


@pytest.mark.parametrize(
    ("long_recordings", "async_preparation", "expected"),
    [(False, False, FOUR_HOURS), (True, False, FOUR_HOURS), (False, True, FOUR_HOURS), (True, True, EIGHT_HOURS)],
)
def test_the_current_limit_reads_the_flags(monkeypatch, long_recordings, async_preparation, expected):
    _flag_defaults(monkeypatch, long_recordings=long_recordings, async_preparation=async_preparation)
    assert current_limit_seconds() == expected


# The settings ---------------------------------------------------------------------------------


def test_the_defaults():
    settings = Settings()
    assert settings.MAX_AUDIO_DURATION_SECONDS == EIGHT_HOURS
    assert settings.PART_TARGET_SECONDS == 12_600
    assert settings.MAX_PARALLEL_PARTS == 4
    assert settings.MAX_UPLOAD_BYTES == 1024**3


@pytest.mark.parametrize("value", [1, 12_600, 14_400])
def test_a_part_target_up_to_azures_limit_is_accepted(value):
    assert Settings(PART_TARGET_SECONDS=value).PART_TARGET_SECONDS == value


@pytest.mark.parametrize("value", [0, -1, 14_401, 28_800])
def test_a_part_target_outside_1_to_14400_is_refused(value):
    with pytest.raises(ValidationError, match="PART_TARGET_SECONDS"):
        Settings(PART_TARGET_SECONDS=value)


@pytest.mark.parametrize("name", ["MAX_AUDIO_DURATION_SECONDS", "MAX_PARALLEL_PARTS", "MAX_UPLOAD_BYTES"])
def test_the_limits_must_be_positive(name):
    with pytest.raises(ValidationError, match=name):
        Settings(**{name: 0})


def test_an_empty_environment_variable_means_the_default(monkeypatch):
    # A deployment template that sets a variable it has no value for must not stop the service starting.
    monkeypatch.setenv("MAX_UPLOAD_BYTES", " ")
    monkeypatch.setenv("PART_TARGET_SECONDS", "")
    settings = Settings()
    assert settings.MAX_UPLOAD_BYTES == 1024**3
    assert settings.PART_TARGET_SECONDS == 12_600


def test_the_upload_backstop_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", str(4 * 1024**3))
    assert Settings().MAX_UPLOAD_BYTES == 4 * 1024**3


def test_a_lease_shorter_than_two_heartbeats_is_refused():
    with pytest.raises(ValidationError, match="PREPARE_LEASE_SECONDS"):
        Settings(PREPARE_LEASE_SECONDS=59, PREPARE_HEARTBEAT_SECONDS=30)
    assert Settings(PREPARE_LEASE_SECONDS=60, PREPARE_HEARTBEAT_SECONDS=30).PREPARE_LEASE_SECONDS == 60
