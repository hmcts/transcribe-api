# HMCTS CNP image for the Transcribe backend.
#
# Follows the platform's Python pattern (see hmcts/cnp-plum-fastapi-backend):
# a slim builder resolves dependencies with uv, and the runtime layer is the
# HMCTS distroless base, which pre-wires the App Insights OpenTelemetry
# distro under /opt/otel. REGISTRY_NAME is supplied by the pipeline via
# `az acr build --build-arg`; the default lets the image build locally.
ARG REGISTRY_NAME=hmctsprod

# ---- ffmpeg: static ffmpeg + ffprobe for the runtime image ----
# Uploads are transcoded with ffmpeg and audio probed with ffprobe; the API and
# worker refuse to start without them. Fully static binaries, so they run on
# distroless. GPLv3 build (mwader/static-ffmpeg), pinned by digest and pulled
# through the hmctsprod ACR cache rule (hmcts/acr-base-importer), not Docker Hub.
FROM ${REGISTRY_NAME}.azurecr.io/imported/mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822 AS ffmpeg

# ---- Builder: resolve and stage dependencies ----
FROM ${REGISTRY_NAME}.azurecr.io/imported/slim/python:3.13-slim-trixie AS builder

# renovate: datasource=github-releases depName=astral-sh/uv
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/

ENV UV_MALWARE_CHECK=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

# git is required at resolve time: hmcts-fastapi-azure-auth has no package-index
# release and is pinned to a git tag (see pyproject.toml).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync \
      --locked \
      --no-dev \
      --no-install-project \
      --python /usr/local/bin/python3.13 \
    && cp -r .venv/lib/python3.13/site-packages /opt/deps

# ---- Runtime: HMCTS distroless base ----
FROM ${REGISTRY_NAME}.azurecr.io/base/python:3.13-distroless

COPY --from=builder /opt/deps /opt/deps
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/

# The project itself is not pip-installed (--no-install-project above); its
# source is laid down directly and reached through PYTHONPATH. The Alembic
# history ships too: the API applies it at startup (runtime/migrations.py).
COPY src/transcribe_api/ /opt/app/transcribe_api/
COPY alembic/ /opt/app/alembic/
COPY alembic.ini pyproject.toml /opt/app/

ENV PYTHONPATH=/opt/otel:/opt/deps:/opt/app \
    PYTHONUNBUFFERED=1

WORKDIR /opt/app

# The base image's entrypoint is the interpreter, so CMD starts at "-m".
# create_app is a factory, hence --factory. Background pollers stay off here
# and run as a separate workload (architecture 4.3, RUN_WORKERS).
CMD ["-m", "uvicorn", "transcribe_api.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
