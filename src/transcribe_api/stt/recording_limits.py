"""How long a recording may be (DIAAT-408): one place that says it.

Azure Speech fails a batch transcription with diarization for a file longer than 240 minutes
(``AZURE_BATCH_FILE_LIMIT_SECONDS``). A longer recording can be accepted only once the service
prepares uploads in the background and splits them into parts, so the limit depends on two system
feature flags (domain/feature_flags.py):

======================  =========================  ==========================================
async_upload_preparation  long_recordings_enabled    effective limit
======================  =========================  ==========================================
off                     off                        min(MAX_AUDIO_DURATION_SECONDS, 240 min)
off                     on                         min(MAX_AUDIO_DURATION_SECONDS, 240 min)
on                      off                        min(MAX_AUDIO_DURATION_SECONDS, 240 min)
on                      on                         MAX_AUDIO_DURATION_SECONDS
======================  =========================  ==========================================

``MAX_AUDIO_DURATION_SECONDS`` below 240 minutes is honoured in every case, so it can still
lower the limit.
"""

from __future__ import annotations

from transcribe_api.domain import feature_flags
from transcribe_api.runtime.settings_recording import AZURE_BATCH_FILE_LIMIT_SECONDS, get_settings


def effective_limit_seconds(*, long_recordings: bool, async_preparation: bool, max_seconds: int) -> int:
    """The longest recording accepted, given the two flags and ``MAX_AUDIO_DURATION_SECONDS``."""
    if long_recordings and async_preparation:
        return max_seconds
    return min(max_seconds, AZURE_BATCH_FILE_LIMIT_SECONDS)


def current_limit_seconds() -> int:
    """The effective limit now, reading the flags (a database read on a cache miss: call it from a
    threadpool on the event loop) and the setting."""
    return effective_limit_seconds(
        long_recordings=feature_flags.is_long_recordings_enabled(),
        async_preparation=feature_flags.is_async_upload_preparation_enabled(),
        max_seconds=get_settings().MAX_AUDIO_DURATION_SECONDS,
    )


def format_duration(seconds: float) -> str:
    """A length for a user-facing message: "4 hours", "4 hours 12 minutes", "45 minutes"."""
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes or not hours:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts)


def too_long_detail(duration: float, max_seconds: float) -> str:
    """The message for a recording over the limit (``duration`` > ``max_seconds``).

    Durations are shown to the minute, so one only seconds over the limit would read
    "4 hours long ... at most 4 hours": then it says it is a little over instead.
    """
    length, limit = format_duration(duration), format_duration(max_seconds)
    if length == limit:
        return f"The recording is a little over {limit} long. Recordings can be at most {limit}."
    return f"The recording is {length} long. Recordings can be at most {limit}."
