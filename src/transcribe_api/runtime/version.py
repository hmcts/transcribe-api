import os
from pathlib import Path

# Written by Jenkinsfile_CNP before the image build (the short git SHA, as in the image
# tag), and copied into the image with the source. The CNP pipeline passes no build
# arguments to the image, so the version cannot come from the environment there.
BUILD_VERSION_FILE = Path(__file__).with_name("BUILD_VERSION")

# 0.0.999 is valid semver that sorts below any real release: a clear "this is local,
# not a real build" signal for /api/health, OpenAPI info and telemetry.
LOCAL_VERSION = "0.0.999"


def read_version() -> str:
    """APP_VERSION if set, else the version baked in at build time, else LOCAL_VERSION."""
    if version := os.getenv("APP_VERSION"):
        return version
    try:
        return BUILD_VERSION_FILE.read_text().strip() or LOCAL_VERSION
    except FileNotFoundError:
        return LOCAL_VERSION


APP_VERSION = read_version()
