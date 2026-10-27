"""Telemetry for transcribe-api (DIAAT-205), built on hmcts-otel.

Call ``configure_telemetry()`` once per process, before the FastAPI app is created
(``api.app.create_app``) or the pollers start (``worker``). It sets up logging (JSON,
redacted), tracing and metrics, exporting to Application Insights when
``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set and to stdout otherwise.

* ``OTEL_SERVICE_NAME`` overrides the service name (``cloud_RoleName``). Unset, the
  API is ``transcribe-api`` and the worker ``transcribe-api-worker``.
* There is no Sentry on CNP: errors go to Application Insights. ``SENTRY_DSN`` is
  ignored even when set.

The base image's ``/opt/otel`` distro is kept off ``PYTHONPATH`` (Dockerfile): it
would configure Azure Monitor a second time at interpreter start.
"""

from __future__ import annotations

import os

from hmcts_otel import TelemetrySettings, default_redactor, setup_telemetry

from transcribe_api.runtime.settings_dictation import get_settings
from transcribe_api.runtime.version import APP_VERSION

SERVICE_NAME_API = "transcribe-api"
SERVICE_NAME_WORKER = "transcribe-api-worker"

# Immigration and Asylum Chamber appeal references, e.g. PA/50123/2024. Uploaded file
# names carry the same reference with underscores (PA_50123_2024.mp3). "_" is a word
# character, so \b would miss references next to other text in file names
# (hearing_PA_50123_2024_part1.mp3): use lookarounds. Case-insensitive.
APPEAL_REFERENCE = r"(?i)(?<![A-Za-z0-9])[A-Z]{2}[/_]\d{5}[/_]\d{4}(?![0-9])"

# Recording uploads are named uploads/{user id}/{upload id}-{sanitised original file
# name}.wav (routes_recording), and the original file name is usually a case
# reference. The Azure SDK exports the blob URL (its HTTP logging policy, and url.full
# on its client spans, of which hmcts-otel strips only the query), and our own blob
# logs print the blob name. Remove whatever follows "/{uuid}/{uuid}-", keeping the IDs
# so the upload stays traceable; plain and percent-encoded paths. No length cap (a cap
# would leak the tail of a long name): the name stops only at a path separator,
# query, quote or space. A name that is already just the marker is skipped, but a
# partly redacted one (after appeal_reference) is still removed whole, so this rule is
# registered first.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_NAME_END = r"[\s/?#\"'<>\\]"
UPLOAD_FILE_NAME = (
    rf"(?:(?<=/{_UUID}/{_UUID}-)|(?<=%2[Ff]{_UUID}%2[Ff]{_UUID}-))"
    rf"(?!\[REDACTED\](?:{_NAME_END}|$))[^\s/?#\"'<>\\]+"
)


def register_redaction_rules() -> None:
    """Add this service's redaction rules, upload file names first. Idempotent."""
    present = {rule.name for rule in default_redactor.rules}
    if "upload_file_name" not in present:
        default_redactor.add_rule("upload_file_name", UPLOAD_FILE_NAME)
    if "appeal_reference" not in present:
        default_redactor.add_rule("appeal_reference", APPEAL_REFERENCE)


def configure_telemetry(service_name: str = SERVICE_NAME_API) -> TelemetrySettings:
    """Set up logging, tracing and metrics for this process. Idempotent.

    ``service_name`` is the default for this process; ``OTEL_SERVICE_NAME`` wins.
    """
    # Checked against the redactor itself, so the rules are present whenever telemetry
    # is configured, whatever happened to the library state before.
    register_redaction_rules()
    overrides: dict[str, object] = {
        "service_version": APP_VERSION,
        "environment": get_settings().ENVIRONMENT,
        # No Sentry on CNP (HMCTS has no Sentry organisation): never initialise it,
        # whatever SENTRY_DSN says.
        "sentry_dsn": None,
    }
    if not any(os.environ.get(name) for name in ("OTEL_SERVICE_NAME", "HMCTS_OTEL_SERVICE_NAME")):
        overrides["service_name"] = service_name
    return setup_telemetry(TelemetrySettings(**overrides))
