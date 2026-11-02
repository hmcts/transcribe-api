"""DIAAT-287: the dictation area's seeded case data for the prohibited-content scan of
captured telemetry (AC3). The engine, pattern classes and evidence file are shared:
tests/unit/telemetry_scan.py.

Everything a test captured with ``hmcts_otel.testing.capture_telemetry`` that would
leave the process (span names, attributes, status descriptions and span events; log
record bodies and attributes, custom events included; metric point attributes) is
flattened into (where, key, text) items and checked for:

* seeded values: the synthetic case details the validation tests put into requests
  (names, case reference, transcript and note text, email addresses, file names,
  a SAS URL and a bearer token);
* pattern classes: anything that looks like an email address, a UK tribunal case
  reference, a SAS signature, a bearer token or JWT, a UK postcode or a National
  Insurance number, whatever its value.

A finding names where and which check matched, never the matched text, so the
evidence file can be published as it is. The RE2-compatible pattern classes
(telemetry_scan.RE2_PATTERNS) are the ones the deployed sample scan uses
(prohibited_content_scan.kql, transcribe-shared-infrastructure).
"""

from __future__ import annotations

import base64

from tests.unit.telemetry_scan import PATTERNS, RE2_PATTERNS, Exported, scan, scan_items  # noqa: F401 - shared
from tests.unit.dictation.unit.api.test_live_transcription_submission_logging import (
    PII_APPELLANT,
    PII_EMAIL,
    PII_JUDGE,
    PII_NOTE,
    PII_RESPONDENT,
    PII_TRANSCRIPT,
)

# The synthetic case the validation tests submit. Built so that each pattern class
# below has a real value in the requests, not only in the scanner's self-test.
CASE_REFERENCE = "PA/50123/2024"
POSTCODE = "SW1A 2AA"
NI_NUMBER = "AB 12 34 56 C"
APPELLANT_EMAIL = "appellant.okonkwo@example.com"
NOTE_TEXT = f"{PII_NOTE} appeal {CASE_REFERENCE}, lives at {POSTCODE}, NI {NI_NUMBER}, contact {APPELLANT_EMAIL}"
TRANSCRIPT_TEXT = f"{PII_TRANSCRIPT} the appellant {PII_APPELLANT} relies on {CASE_REFERENCE}"
DOWNLOAD_FILENAME = f"[2026-01-15] {PII_APPELLANT} v {PII_RESPONDENT}.docx"
# Synthetic credentials, built at runtime so no secret scanner mistakes them for real ones.
SAS_SIGNATURE = "sig=" + base64.b64encode(b"validation-signature").decode().replace("=", "%3D")
SAS_URL = f"https://teststorage.blob.core.windows.net/c/hearing.docx?sv=2024-08-04&se=2026-01-15&{SAS_SIGNATURE}"
# A syntactically valid, unsigned JWT: never a real token.
BEARER_TOKEN = ".".join(
    base64.urlsafe_b64encode(part).decode().rstrip("=")
    for part in (b'{"alg":"none"}', b'{"sub":"validation"}', b"validation-signature")
)

# label -> value. Labels (never values) go into the evidence file.
SEEDED: dict[str, str] = {
    "appellant_name": PII_APPELLANT,
    "respondent_name": PII_RESPONDENT,
    "judge_name": PII_JUDGE,
    "judicial_note": PII_NOTE,
    "transcript_text": PII_TRANSCRIPT,
    "user_email": PII_EMAIL,
    "appellant_email": APPELLANT_EMAIL,
    "case_reference": CASE_REFERENCE,
    "postcode": POSTCODE,
    "ni_number": NI_NUMBER,
    "download_filename": DOWNLOAD_FILENAME,
    "sas_signature": SAS_SIGNATURE,
    "bearer_token": BEARER_TOKEN,
}
