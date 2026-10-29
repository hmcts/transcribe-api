"""DIAAT-287 AC3: the recording area's seeded case data for the prohibited-content scan
(the engine and pattern classes are shared: tests/unit/telemetry_scan.py).

Independent of the service's redaction rules: if a rule is removed or a new code path
logs something it should not, the scan still fails. Two kinds of check:

* seeded values: the realistic case data the validation scenarios feed in (file
  names with a case reference and party names, transcript text, speaker names, blob
  paths, SAS signatures, emails, Azure's free-text error message, a bearer token).
  None of them may appear anywhere;
* pattern classes: emails, UK case references, SAS signatures, bearer tokens and JWTs,
  UK postcodes and National Insurance numbers. No match may appear anywhere, whatever
  the value.

Findings name the seed or pattern and where it was found, never the matched text, so
the evidence file can be attached to the release evidence as it is.

Every seeded value is built at runtime (concatenation): a literal in a source line
would reach telemetry through any exception's stack trace and fail the scan for the
wrong reason.
"""

from __future__ import annotations

import re

from hmcts_otel.testing import TelemetryCapture

from tests.unit.telemetry_scan import (  # noqa: F401 - re-exported for the scenarios
    PATTERNS,
    Counts,
    Exported,
    Finding,
    ScanResult,
    exported_items,
)
from tests.unit.telemetry_scan import scan as _scan

# Seeded values ----------------------------------------------------------------------------

CASE_REFERENCE = "PA" + "/50123/" + "2024"  # an Immigration and Asylum Chamber appeal
CASE_REFERENCE_IN_FILE = "PA" + "_50123_" + "2024"
APPELLANT_GIVEN_NAME = "Ade" + "bayo"  # one word: fits identifier-shaped attributes
APPELLANT = APPELLANT_GIVEN_NAME + " Okonkwo-" + "Hargreaves"
APPELLANT_SURNAME = "Okonkwo-" + "Hargreaves"
RESPONDENT = "Secretary of State " + "for the Home Department"
SPEAKER_NAME = "Judge " + "Fairweather-Lindqvist"
USER_EMAIL = "clerk." + "one" + "@" + "justice.example"
PARTY_EMAIL = "adebayo.okonkwo" + "@" + "mail.example"
POSTCODE = "SW1A" + " 2AA"
NI_NUMBER = "AB" + " 12 34 56 " + "C"
SAS_SIGNATURE = "c2VjcmV0" + "LXNpZ25hdHVyZS1mb3ItdGVzdA"
SPEECH_KEY = "5f1e0c" + "9a7b3d4e8f2a6c1b0d9e7f3a"  # the Azure Speech subscription key
BEARER_TOKEN = "eyJ" + "hbGciOiJSUzI1NiJ9" + "." + "eyJ" + "zdWIiOiJjbGVyayJ9" + "." + "c2lnbmF0dXJlLXZhbHVl"

# What the user's file is called, and what the upload stores it as.
ORIGINAL_FILE_NAME = f"{CASE_REFERENCE_IN_FILE} {APPELLANT} v Home Office.mp3"
FILE_STEM = f"{CASE_REFERENCE_IN_FILE}_" + APPELLANT.replace(" ", "_") + "_v_Home_Office"
TRANSCRIPT_TEXT = (
    f"My name is {APPELLANT}. I live at 4 Acacia Avenue, {POSTCODE}. "
    f"My national insurance number is {NI_NUMBER} and my email is {PARTY_EMAIL}."
)
SPEAKER_TEXT = f"I am {SPEAKER_NAME} and this is the appeal of {APPELLANT} against the {RESPONDENT}."
AZURE_ERROR_MESSAGE = (
    f"Cannot transcribe {FILE_STEM}.wav for {APPELLANT}: the recordings URI "
    f"https://teststorage.blob.core.windows.net/x/{FILE_STEM}.wav"
    f"?sv=2024-05-04&sig={SAS_SIGNATURE} "
    "contains invalid data"
)

# label -> value. Labels go in the evidence; values never do.
SEEDED: dict[str, str] = {
    "case_reference": CASE_REFERENCE,
    "case_reference_in_file_name": CASE_REFERENCE_IN_FILE,
    "appellant_name": APPELLANT,
    "appellant_given_name": APPELLANT_GIVEN_NAME,
    "appellant_surname": APPELLANT_SURNAME,
    "respondent_name": RESPONDENT,
    "speaker_name": SPEAKER_NAME,
    "original_file_name": ORIGINAL_FILE_NAME,
    "stored_file_stem": FILE_STEM,
    "transcript_text": TRANSCRIPT_TEXT,
    "speaker_text": SPEAKER_TEXT,
    "user_email": USER_EMAIL,
    "party_email": PARTY_EMAIL,
    "postcode": POSTCODE,
    "ni_number": NI_NUMBER,
    "sas_signature": SAS_SIGNATURE,
    "bearer_token": BEARER_TOKEN,
    "speech_subscription_key": SPEECH_KEY,
    "azure_error_message": AZURE_ERROR_MESSAGE,
}

# Scan -------------------------------------------------------------------------------------


def scan(
    tel: TelemetryCapture,
    seeded: dict[str, str] | None = None,
    patterns: dict[str, re.Pattern[str]] | None = None,
) -> ScanResult:
    """The shared scan with the recording area's seeded values by default."""
    return _scan(tel, SEEDED if seeded is None else seeded, patterns)
