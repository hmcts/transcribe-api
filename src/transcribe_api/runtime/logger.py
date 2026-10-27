import logging

from hmcts_otel import setup_logging

from transcribe_api.runtime.telemetry import register_redaction_rules


def setup_logger():
    # Full telemetry (exporters) is configured once per process by
    # runtime/telemetry.configure_telemetry (api/app.py, worker.py). Processes that
    # import app modules without either (scripts, one-off jobs) still need redacted
    # INFO logs: setup_logging() installs logging only, with no exporters or provider
    # checks, so it is safe at import time, and the later full setup replaces it. If
    # another component owns logging (e.g. alembic's fileConfig) it only adds redaction
    # to that component's handlers. Never basicConfig: it would add an unformatted,
    # unredacted second handler.
    register_redaction_rules()
    setup_logging()

    # Suppress verbose Azure SDK logging
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    logging.getLogger("azure").setLevel(logging.WARNING)

    return logging.getLogger(__name__)


logger = setup_logger()
