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
