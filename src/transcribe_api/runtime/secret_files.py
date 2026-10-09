"""Load Key Vault secrets mounted into the pod by the CNP Helm chart.

The HMCTS `python` base chart's `keyVaults:` block does NOT create environment
variables. It provisions a SecretProviderClass and mounts each secret as a FILE
at `/mnt/secrets/<vault>/<alias>` via the Azure Key Vault CSI driver. Each alias
in charts/transcribe-api/values.yaml is spelled as the environment variable it
stands for.

Everything that reads configuration here reads the ENVIRONMENT — our two
pydantic-settings classes, and hmcts-fastapi-azure-auth's own AuthSettings,
which builds the JWT verifier from AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID. An
earlier version pointed only our settings classes at this directory
(pydantic's secrets_dir); the library would never have seen the tenant ID and
every authenticated request would have failed. Loading the files into
os.environ once, at package import, gives every consumer the same view.

A variable already present in the environment is never overwritten, so an
explicit env var (local runs, tests) still wins. The directory is probed, so
local runs and the test suite, which have no mount, are unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path

# Matches `keyVaults: transcribe:` in charts/transcribe-api/values.yaml.
DEFAULT_SECRETS_DIR = "/mnt/secrets/transcribe"


def load_secret_files_into_environ(directory: str | None = None) -> list[str]:
    """Copy each mounted secret file into os.environ. Returns the names loaded."""
    root = Path(directory or os.environ.get("SECRETS_DIR", DEFAULT_SECRETS_DIR))
    if not root.is_dir():
        return []

    loaded: list[str] = []
    for entry in sorted(root.iterdir()):
        # The CSI driver also creates ..data / ..timestamp bookkeeping entries.
        if entry.name.startswith(".") or not entry.is_file():
            continue
        if entry.name in os.environ:
            continue
        os.environ[entry.name] = entry.read_text().strip()
        loaded.append(entry.name)
    return loaded
