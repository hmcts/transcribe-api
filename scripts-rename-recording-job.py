#!/usr/bin/env python3
"""Rename the recording bounded context's `TranscriptionJob` to `SpeechBatchJob`.

Why: both upstream services define a class called `TranscriptionJob`. They do NOT
collide at the table level (recording pins `transcription_job`, dictation defaults
to `transcriptionjob`), but they DO collide in SQLAlchemy's declarative class
registry, which makes relationship strings like "TranscriptionJob" ambiguous:

    InvalidRequestError: Multiple classes found for path "TranscriptionJob"
    in the registry of this declarative base.

Architecture 3.3 records that these are different concepts wearing the same word:
recording's is an Azure Speech *batch* job (batch_job_id, batch_job_url,
model_identifier, webhook_dispatched_at, idempotency_key, caller_id), dictation's
is a unit of dictation work bound to its Transcription aggregate. So the recording
class gets the accurate name and dictation keeps the generic one.

`__tablename__` is deliberately left as "transcription_job" so no database
migration is needed for existing recording data.

Scope: only modules and tests that resolve the symbol from the recording
modules. The dictation context is untouched.
"""
from __future__ import annotations

import pathlib
import re
import sys

OLD, NEW = "TranscriptionJob", "SpeechBatchJob"

# Files whose `TranscriptionJob` refers to the recording context.
RECORDING_SRC = [
    "src/transcribe_api/domain/models_recording.py",
    "src/transcribe_api/domain/interface_recording.py",
    "src/transcribe_api/domain/webhook.py",
    "src/transcribe_api/api/routes_recording.py",
    "src/transcribe_api/api/deps_recording.py",
    "src/transcribe_api/stt/submission.py",
    "src/transcribe_api/stt/polling_service.py",
    "src/transcribe_api/stt/accuracy.py",
    "src/transcribe_api/stt/wer.py",
    "src/transcribe_api/stt/batch_client.py",
]
RECORDING_TESTS_DIR = "tests/recording"


def rewrite(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    text = path.read_text()
    # Word-boundary rename. Leaves __tablename__ = "transcription_job" alone
    # because that is a string literal, not the identifier.
    new_text, n = re.subn(rf"\b{OLD}\b", NEW, text)
    if n:
        path.write_text(new_text)
    return n


def main() -> int:
    root = pathlib.Path(__file__).parent
    total = 0
    touched = []

    for rel in RECORDING_SRC:
        n = rewrite(root / rel)
        if n:
            total += n
            touched.append(f"{rel} ({n})")

    for p in (root / RECORDING_TESTS_DIR).rglob("*.py"):
        n = rewrite(p)
        if n:
            total += n
            touched.append(f"{p.relative_to(root)} ({n})")

    print(f"renamed {OLD} -> {NEW}: {total} references")
    for t in touched:
        print(f"   {t}")

    # Safety: the table name must be unchanged.
    models = (root / "src/transcribe_api/domain/models_recording.py").read_text()
    assert '__tablename__ = "transcription_job"' in models, "table name changed — would need a migration!"
    print('   verified __tablename__ still "transcription_job" (no migration needed)')
    return 0


if __name__ == "__main__":
    sys.exit(main())
