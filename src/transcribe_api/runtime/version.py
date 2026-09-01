import os

# Set at image build time via Dockerfile ARG VERSION → ENV APP_VERSION.
# CI passes the artefact version from hmcts/artefact-version-action@v1, so
# release images carry a clean semver and draft images carry <semver>-<sha>.
# 0.0.999 is valid semver that sorts below any real release — a clear
# "this is local, not a real build" signal for /api/health and OpenAPI info.
APP_VERSION = os.getenv("APP_VERSION", "0.0.999")
