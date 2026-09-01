import os
from functools import cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    API_PORT: int = 8080
    APP_URL: str
    AZURE_AD_CLIENT_ID: str
    AZURE_AD_TENANT_ID: str
    AZURE_GROK_API_KEY: str | None = None
    AZURE_GROK_ENDPOINT: str | None = None
    AZURE_OPENAI_API_KEY: str | None = None
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_ENDPOINT: str = ""
    # Full Azure resource ID for the Speech resource — required when public network
    # access is disabled and Managed Identity auth must be used instead of API keys.
    # Format: /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<name>
    AZURE_SPEECH_RESOURCE_ID: str | None = None
    # Azure Storage Configuration (uses Managed Identity)
    AZURE_STORAGE_ACCOUNT_NAME: str
    AZURE_STORAGE_CONTAINER_NAME: str
    AZURE_STORAGE_TRANSCRIPTION_CONTAINER: str
    DATABASE_CONNECTION_STRING: str
    ENVIRONMENT: str = "local"
    # Onboarding Override for Development Testing
    FORCE_ONBOARDING_DEV: bool = False
    # Override the default app roles (JSON dictionary of role value strings)
    AUTH_APPROLES: str | None = None
    GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT: str | None = None
    GOV_NOTIFY_API_KEY: str | None = None
    # JWT Verification Settings - Strict by default
    JWT_ENABLE_VERIFICATION: bool = True
    JWT_VERIFICATION_STRICT: bool = True
    DISABLE_FASTAPI_INSTRUMENTATION: bool = False
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_HOST: str | None = None
    # When False (default), service endpoints returned to the frontend are rewritten
    # to the app's own domain so all Azure SDK traffic flows through the Caddy proxy.
    # Set True to let the frontend talk directly to Azure services.
    DIRECT_SDK_ACCESS: bool = False
    # SAS URL TTL configuration.
    # Download: 60 minutes balances security (short window) with usability (large files).
    # Upload: 120 minutes covers large courtroom audio files on slow connections.
    # Both are configurable per environment via App Service settings without code change.
    DOWNLOAD_URL_TTL_MINUTES: int = 60
    UPLOAD_URL_TTL_MINUTES: int = 120
    RUN_MIGRATIONS: bool = False
    SENTRY_DSN: str | None = None
    # CORS configuration from infrastructure
    CORS_ALLOWED_ORIGINS: str | None = None
    # Transcription polling service configuration

    @field_validator("LANGFUSE_HOST")
    @classmethod
    def validate_langfuse_host(cls, v):
        """Validate that only the approved Justice AI Unit Langfuse instance is used."""
        if not v:
            return v
        allowed_host = "https://langfuse-ai.justice.gov.uk"
        if v != allowed_host:
            error_msg = (
                f"Disallowed Langfuse host '{v}'. Only {allowed_host} is permitted. "
                f"This prevents accidental data leakage to unauthorized instances."
            )
            raise ValueError(error_msg)
        return v


class LocalSettings(Settings):
    """Settings class that loads from .env file for local development."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class ProductionSettings(Settings):
    """Settings class for production environments (no .env file)."""

    model_config = SettingsConfigDict(extra="ignore")


@cache
def get_settings(environment: str | None = None):
    """Get Settings instance with optional environment override.

    Args:
        environment: Override environment setting. If None, uses ENVIRONMENT env var or default.

    Returns:
        Settings: Configured settings instance.
    """
    # Handle None case for lru_cache compatibility
    env_key = environment if environment is not None else os.getenv("ENVIRONMENT", "local")

    # Choose the appropriate settings class based on environment
    # Use LocalSettings for local and test environments (allows .env file and mocked values)
    if env_key in ("local", "test"):
        return LocalSettings()
    else:
        return ProductionSettings(ENVIRONMENT=env_key)
