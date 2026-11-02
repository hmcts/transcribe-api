"""runtime/logger.py must not load the database layer (DIAAT-276 review): scripts and
modules that only log should not import SQLModel or the ORM models."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_logger_does_not_import_the_orm():
    code = (
        "import sys, transcribe_api.runtime.logger, transcribe_api.runtime.telemetry; "
        "loaded = [m for m in ('transcribe_api.domain.models_dictation', 'sqlmodel') if m in sys.modules]; "
        "print(loaded); sys.exit(1 if loaded else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
