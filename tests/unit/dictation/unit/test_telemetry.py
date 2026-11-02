"""DIAAT-205: app-level telemetry configuration (hmcts-otel)."""

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hmcts_otel import default_redactor
from hmcts_otel.testing import capture_telemetry

from transcribe_api.runtime.telemetry import configure_telemetry


def test_appeal_references_are_redacted_everywhere():
    configure_telemetry()
    assert "appeal_reference" in [rule.name for rule in default_redactor.rules]
    for reference in ("PA/50123/2024", "HU_01234_2023"):
        assert reference not in default_redactor.redact(f"processing {reference}.mp3")

    with capture_telemetry() as telemetry:
        logging.getLogger("app.audio").info("uploaded PA_50123_2024.mp3 for job")
        telemetry.assert_no_prohibited_content("PA_50123_2024")


def test_configure_telemetry_is_idempotent():
    first = configure_telemetry()
    assert configure_telemetry() is first
    assert [rule.name for rule in default_redactor.rules].count("appeal_reference") == 1


@pytest.mark.parametrize(
    "text",
    [
        "uploaded hearing_PA_50123_2024.mp3",
        "PA_50123_2024_part1.mp3",
        "ref pa/50123/2024 listed",
        "(HU/01234/2023)",
    ],
)
def test_appeal_reference_redacted_next_to_underscores_and_in_lower_case(text):
    configure_telemetry()
    out = default_redactor.redact(text)
    assert "50123" not in out
    assert "01234" not in out


def test_appeal_reference_rule_leaves_other_numbers_alone():
    configure_telemetry()
    text = "job 123456 took 2024 ms; build 1.2.3"
    assert default_redactor.redact(text) == text


def test_scripts_without_main_still_log_info_redacted():
    """Processes that import app modules without main (scripts/) keep INFO logs, redacted."""
    root = Path(__file__).resolve().parents[4]
    code = "from transcribe_api.runtime.logger import logger; logger.info('cleanup for %s on PA_50123_2024', 'judge@justice.gov.uk')"
    env = {k: v for k, v in os.environ.items() if k != "APPLICATIONINSIGHTS_CONNECTION_STRING"}
    env["PYTHONPATH"] = str(root / "src")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "cleanup for [REDACTED] on [REDACTED]" in result.stdout
    assert "judge@justice.gov.uk" not in result.stdout + result.stderr
