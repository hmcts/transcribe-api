from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Core
    ENVIRONMENT: str = "local"
    # Git commit SHA of the deployed build, baked into the container image at
    # build time (Dockerfile ARG GIT_SHA -> ENV GIT_SHA). Defaults to "unknown"
    # so local/dev runs without the build arg still work. Exposed via
    # GET /api/v1/version so post-deploy checks can confirm the live build.
    GIT_SHA: str = "unknown"
    DATABASE_CONNECTION_STRING: str = (
        "postgresql://dev:devpass@localhost:5432/transcription_svc"  # pragma: allowlist secret
    )

    # Azure Speech
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_ENDPOINT: str = ""
    AZURE_SPEECH_RESOURCE_ID: str | None = None

    # Azure Storage
    AZURE_STORAGE_ACCOUNT_NAME: str = ""
    AZURE_STORAGE_CONTAINER_NAME: str = ""
    # Set in local dev to avoid mounting ~/.azure into Docker.
    # Leave unset in deployed envs — DefaultAzureCredential uses Managed Identity.
    AZURE_STORAGE_CONNECTION_STRING: str | None = None

    # Local audio storage (dev only — never set in deployed envs)
    AUDIO_STORAGE_BACKEND: Literal["azure", "local"] = "azure"
    LOCAL_AUDIO_STORAGE_DIR: str = "/tmp/local-audio"  # noqa: S108  # nosec B108
    LOCAL_AUDIO_BASE_URL: str = ""

    # Polling
    BATCH_POLL_INTERVAL_SECONDS: int = 30
    BATCH_TRANSCRIPTION_THRESHOLD_HOURS: float = 2.0

    # Azure AD / JWT authentication
    AZURE_AD_CLIENT_ID: str = ""
    AZURE_AD_TENANT_ID: str = ""
    JWT_ENABLE_VERIFICATION: bool = True
    JWT_VERIFICATION_STRICT: bool = True
    AUTH_APPROLES: str | None = None

    # Auth
    LOCAL_API_KEY: str = "local-dev-key-change-me"
    # Fernet key (URL-safe base64, 32 bytes) used to encrypt webhook_secret at rest.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # noqa: E501
    # The default is a fixed all-zeros key for local development only.
    WEBHOOK_SECRET_ENCRYPTION_KEY: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="  # noqa: S105  # gitleaks:allow

    # Webhook delivery
    WEBHOOK_TIMEOUT_SECONDS: float = 30.0
    WEBHOOK_MAX_RETRIES: int = 3

    # Optional per-environment override for the review-highlighting cutoff
    # (see audio/accuracy.py DEFAULT_CONFIDENCE_THRESHOLD). Expressed as a
    # 0-1 ratio, matching Azure's per-word confidence scale — NOT a percent.
    LOW_CONFIDENCE_THRESHOLD: float | None = None

    # Corrections dataset export (DIAAT-231)
    # Gates whether clerk corrections (word-range and whole-segment) are
    # additionally persisted into the correction_dataset_entry table, a
    # durable store separate from per-job transcript data meant for later
    # export to fine-tune/evaluate transcription models.
    #
    # Defaults to False: this table can capture real court-hearing content,
    # and retention/anonymisation policy for it has NOT yet been defined or
    # signed off by legal/compliance (see DIAAT-231 acceptance criteria #3,
    # and the docstring on CorrectionDatasetEntry). Do not set this to True
    # in any environment handling real hearing content until that sign-off
    # has happened — until then, the write path exists and is testable, but
    # stays off so no real transcript content is silently captured.
    CORRECTIONS_DATASET_EXPORT_ENABLED: bool = False

    # Observability
    SENTRY_DSN: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("BATCH_TRANSCRIPTION_THRESHOLD_HOURS")
    @classmethod
    def validate_threshold(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("BATCH_TRANSCRIPTION_THRESHOLD_HOURS must be greater than 0")
        return v

    @field_validator("LOW_CONFIDENCE_THRESHOLD")
    @classmethod
    def validate_low_confidence_threshold(cls, v: float | None) -> float | None:
        # Guard against the common percent-vs-ratio misconfiguration (e.g.
        # setting 65 instead of 0.65, which would flag every word). Azure's
        # per-word confidence is a 0-1 ratio; 0.0 (flag nothing) is allowed.
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("LOW_CONFIDENCE_THRESHOLD must be a ratio between 0 and 1")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
