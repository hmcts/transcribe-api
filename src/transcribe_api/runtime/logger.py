import logging

from hmcts_otel import setup_logging

from transcribe_api.runtime.telemetry import register_redaction_rules


def setup_logger():
    # Scripts and one-off jobs import app modules without configure_telemetry() and
    # still need redacted logs. setup_logging() installs logging only (no exporters),
    # so it is safe at import time; the full setup replaces it later. Not basicConfig:
    # it would add a second, unredacted handler.
    register_redaction_rules()
    setup_logging()

    # Suppress verbose Azure SDK logging
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    logging.getLogger("azure").setLevel(logging.WARNING)

    return logging.getLogger(__name__)


logger = setup_logger()
