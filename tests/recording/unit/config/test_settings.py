"""Unit tests for Settings validation."""

import pytest
from pydantic import ValidationError

from transcribe_api.runtime.settings_recording import Settings


class TestLowConfidenceThreshold:
    # DIAAT-235: the override is a 0-1 ratio, matching Azure's per-word
    # confidence scale. Guard against the common percent-vs-ratio mistake.
    def test_unset_is_allowed(self):
        assert Settings(LOW_CONFIDENCE_THRESHOLD=None).LOW_CONFIDENCE_THRESHOLD is None

    def test_valid_ratio_is_accepted(self):
        assert Settings(LOW_CONFIDENCE_THRESHOLD=0.65).LOW_CONFIDENCE_THRESHOLD == 0.65

    def test_zero_is_accepted(self):
        # 0.0 means "flag nothing" — a legitimate, intentional value.
        assert Settings(LOW_CONFIDENCE_THRESHOLD=0.0).LOW_CONFIDENCE_THRESHOLD == 0.0

    def test_one_is_accepted(self):
        assert Settings(LOW_CONFIDENCE_THRESHOLD=1.0).LOW_CONFIDENCE_THRESHOLD == 1.0

    def test_percent_style_value_is_rejected(self):
        # 65 (percent) instead of 0.65 (ratio) would flag every word.
        with pytest.raises(ValidationError):
            Settings(LOW_CONFIDENCE_THRESHOLD=65)

    def test_negative_value_is_rejected(self):
        with pytest.raises(ValidationError):
            Settings(LOW_CONFIDENCE_THRESHOLD=-0.1)
