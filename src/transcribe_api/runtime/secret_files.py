"""Locate Key Vault secrets mounted into the pod by the CNP Helm chart.

The HMCTS `python` base chart's `keyVaults:` block does NOT create environment
variables. It provisions a SecretProviderClass and mounts each secret as a FILE
under `/mnt/secrets/<vault>/<alias>` via the Azure Key Vault CSI driver.

pydantic-settings reads exactly that shape through `secrets_dir`, so each
chart-side `alias` is simply the settings field name. Precedence is unchanged:
real environment variables still win over files, which is what keeps local
development and the test suite behaving as before.

The directory is probed rather than hard-coded so that local runs, the test
suite and the worker entrypoint — none of which have the mount — skip it
instead of tripping pydantic-settings' "directory does not exist" warning.
"""

from __future__ import annotations

import os
from pathlib import Path

# Matches `keyVaults: transcribe:` in charts/transcribe-api/values.yaml.
DEFAULT_SECRETS_DIR = "/mnt/secrets/transcribe"


def secrets_dir() -> str | None:
    """Return the mounted secrets directory, or None when it is not present."""
    candidate = os.environ.get("SECRETS_DIR", DEFAULT_SECRETS_DIR)
    return candidate if Path(candidate).is_dir() else None
