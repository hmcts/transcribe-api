# ruff: noqa: TRY003, TRY300, EM101
import json
import logging
from collections.abc import Callable
from enum import StrEnum
from tomllib import load
from typing import Any

from langfuse import Langfuse
from langfuse.decorators import langfuse_context, observe
from litellm import acompletion
from pydantic import BaseModel
from pathlib import Path

from pyprojroot import here

from transcribe_api.runtime.settings_dictation import get_settings

logger = logging.getLogger(__name__)

# Load config from file in the same directory as this module
# MERGE NOTE: was pyprojroot.here("app/llm/llm_client.toml"), i.e. relative to
# the project root under the old layout. Module-relative is correct here and
# also survives being installed as a wheel or run from a container, which the
# pyprojroot form did not.
_config_path = Path(__file__).parent / "llm_client.toml"
with _config_path.open("rb") as f:
    toml_data = load(f)

llm_params = toml_data["llm_params"]


class LLMModel(StrEnum):
    AZURE_GPT_4O = "azure/gpt-4o-2024-08-06"
    AZURE_GROK_3 = "azure/grok-3"
    VERTEX_GEMINI_25_PRO = "vertex_ai/gemini-2.5-pro"
    VERTEX_GEMINI_20_FLASH = "vertex_ai/gemini-2.0-flash"
    VERTEX_GEMINI_25_FLASH = "vertex_ai/gemini-2.5-flash"


ALL_LLM_MODELS = [
    LLMModel.AZURE_GPT_4O,
    LLMModel.AZURE_GROK_3,
    LLMModel.VERTEX_GEMINI_25_PRO,
    LLMModel.VERTEX_GEMINI_20_FLASH,
    LLMModel.VERTEX_GEMINI_25_FLASH,
]

# Create langfuse client with settings
_settings = get_settings()

langfuse_client = Langfuse(
    public_key=_settings.LANGFUSE_PUBLIC_KEY,
    secret_key=_settings.LANGFUSE_SECRET_KEY,
    host=_settings.LANGFUSE_HOST,
    environment=_settings.ENVIRONMENT,
)

langfuse_context.configure(environment=_settings.ENVIRONMENT)


class LangfuseAuthManager:
    """Manages Langfuse authentication state and lazy initialization."""

    def __init__(self):
        self._authenticated = False

    def ensure_authenticated(self):
        """
        Ensure Langfuse client is authenticated.

        This is called lazily on first use rather than at import time
        to avoid authentication failures during test collection.
        """
        if not self._authenticated:
            auth_result = langfuse_client.auth_check()
            if not auth_result:
                raise RuntimeError(
                    f"Langfuse authentication failed for host: {get_settings().LANGFUSE_HOST}. "
                    f"Please verify your LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are correct."
                )
            self._authenticated = True


# Create singleton instance
_auth_manager = LangfuseAuthManager()


def _ensure_langfuse_authenticated():
    """Convenience function to ensure Langfuse authentication."""
    _auth_manager.ensure_authenticated()


def _load_vertex_credentials() -> str:
    """
    Load Google Cloud credentials from env vars. Returns the credentials
    as a JSON string.
    """
    try:
        vertex_credentials = get_settings().GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT
        return vertex_credentials
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT: {e!s}") from e


async def gemini_eu_fallback_acompletion(*, model: str, messages: list, **kwargs):
    """
    Gemini completion with EU region fallback.

    Tries multiple EU regions in order until one succeeds. This handles
    cases where a specific region might be temporarily unavailable.
    """
    safety_categories = llm_params["GEMINI_SAFETY_OVERRIDE_CATEGORIES"]
    last_exception = None
    for region in llm_params["VERTEX_LOCATIONS"]:
        try:
            logger.info("Attempting Gemini completion with model %s in region %s", model, region)
            result = await acompletion(
                model=model,
                messages=messages,
                fallbacks=[LLMModel.VERTEX_GEMINI_25_FLASH, LLMModel.VERTEX_GEMINI_20_FLASH],
                max_retries=llm_params["NUM_RETRIES"],
                retry_strategy=llm_params["RETRY_STRATEGY"],
                safety_settings=[{"category": _cat, "threshold": "BLOCK_NONE"} for _cat in safety_categories],
                timeout=llm_params["TIMEOUT"],
                vertex_credentials=_load_vertex_credentials(),
                vertex_location=region,
                **kwargs,
            )
            # Basic success check
            if result and result.choices and getattr(result.choices[0].message, "content", None):
                logger.info("Successfully completed Gemini request in region %s", region)
                return result
            else:
                logger.warning("Gemini completion contained no content")
                raise ValueError("Gemini completion contained no content")

        except Exception as exc:
            logger.warning("Gemini completion failed in region %s: %s", region, exc)
            last_exception = exc
            continue
    # If none succeed, raise last error
    logger.error("All Gemini regions failed. Last error: %s", last_exception)
    raise last_exception


async def azure_grok_acompletion(*, model: str, messages: list, **kwargs):
    """
    Azure Grok completion with standard retry strategy.

    Provides a consistent interface for Azure Grok API calls with
    pre-configured authentication and retry parameters.
    """
    settings = get_settings()
    logger.info("Attempting Azure Grok completion with model %s", model)
    result = await acompletion(
        model=model,
        messages=messages,
        api_base=settings.AZURE_GROK_ENDPOINT,
        api_version=llm_params["AZURE_GROK_API_VERSION"],
        api_key=settings.AZURE_GROK_API_KEY,
        num_retries=llm_params["NUM_RETRIES"],
        timeout=llm_params["TIMEOUT"],
        retry_strategy=llm_params["RETRY_STRATEGY"],
        **kwargs,
    )
    logger.info("Successfully completed Azure Grok request")
    return result


async def azure_acompletion(*, model: str, messages: list, **kwargs):
    """
    Azure OpenAI completion with standard retry strategy.

    Provides a consistent interface for Azure OpenAI API calls with
    pre-configured authentication and retry parameters.
    """
    settings = get_settings()
    logger.info("Attempting Azure OpenAI completion with model %s", model)
    result = await acompletion(
        model=model,
        messages=messages,
        api_base=settings.AZURE_OPENAI_ENDPOINT,
        api_version=llm_params["AZURE_OPENAI_API_VERSION"],
        api_key=settings.AZURE_OPENAI_API_KEY,
        num_retries=llm_params["NUM_RETRIES"],
        timeout=llm_params["TIMEOUT"],
        retry_strategy=llm_params["RETRY_STRATEGY"],
        **kwargs,
    )
    logger.info("Successfully completed Azure OpenAI request")
    return result


def _is_content_filtering_error(error: Exception) -> bool:
    """
    Detect if an error is specifically a CSAM content filtering error from Gemini.
    """
    error_str = str(error).lower()

    # CSAM-specific content filtering error patterns from Gemini/Vertex AI
    csam_filtering_indicators = [
        "harm_category_sexually_explicit",
        "sexually_explicit",
        "child safety",
        "child exploitation",
        "sexual content involving minors",
        "underage",
        "minor safety",
        "csam",
        "child sexual abuse",
        "child abuse material",
    ]

    return any(indicator in error_str for indicator in csam_filtering_indicators)


async def _completion_with_multi_fallback(*, model: str, messages: list, **kwargs):
    """
    Gemini completion with automatic multi-level fallbacks:
    1. Try Gemini first
    2. On CSAM content filtering errors, fallback to Azure Grok
    3. If Grok also fails, fallback to Azure OpenAI GPT-4o
    This handles cases where Gemini blocks legitimate legal/judicial content mentioning CSAM.
    """
    try:
        # Try Gemini first
        result = await gemini_eu_fallback_acompletion(model=model, messages=messages, **kwargs)
        # Update observation to track successful Gemini usage
        langfuse_context.update_current_observation(
            metadata={
                "actual_model_used": model,
                "fallback_triggered": False,
                "fallback_reason": None,
            }
        )
        if not result.choices or result.choices[0].message.content is None or result.choices[0].message.content == "":
            logger.warning("Gemini returned no content")
            raise ValueError("Gemini returned no content")
        return result
    except Exception as e:
        if _is_content_filtering_error(e) or "Gemini returned no content" in str(e):
            try:
                # Fall back to Azure Grok
                logger.info("Falling back to Azure Grok due to Gemini content filtering")
                result = await azure_grok_acompletion(model=LLMModel.AZURE_GROK_3, messages=messages, **kwargs)
                # Update observation to track Grok fallback usage
                langfuse_context.update_current_observation(
                    metadata={
                        "requested_model": model,
                        "actual_model_used": LLMModel.AZURE_GROK_3,
                        "fallback_triggered": True,
                        "fallback_reason": "gemini_content_filtering",
                        "original_error": str(e),
                    }
                )
                return result
            except Exception as grok_error:
                # If Grok also fails, fall back to Azure OpenAI GPT-4o as final fallback
                result = await azure_acompletion(model=LLMModel.AZURE_GPT_4O, messages=messages, **kwargs)
                # Update observation to track final fallback usage
                langfuse_context.update_current_observation(
                    metadata={
                        "requested_model": model,
                        "actual_model_used": LLMModel.AZURE_GPT_4O,
                        "fallback_triggered": True,
                        "fallback_reason": "gemini_content_filtering_and_grok_failure",
                        "original_error": str(e),
                        "grok_error": str(grok_error),
                    }
                )
                return result
        # If it's not a CSAM content filtering error, raise the original error
        raise


def with_structured_output(completion_func: Callable) -> Callable:
    """
    Wraps a completion function to handle structured output using Pydantic models.
    Works with any completion function that returns a standard completion response.
    Retries up to 5 times if JSON validation fails by requesting a new completion.
    """

    async def wrapped(messages: list, response_format: type[BaseModel] | None = None, **kwargs: Any):
        max_attempts = 5
        attempt = 0

        while attempt < max_attempts:
            try:
                response = await completion_func(messages=messages, response_format=response_format, **kwargs)
                content = response.choices[0].message.content

                if response_format:
                    return response_format.model_validate_json(content)
                return content
            except (json.JSONDecodeError, ValueError):
                attempt += 1
                if attempt == max_attempts:
                    raise  # Re-raise the last exception if we've exhausted all attempts
                continue

        return None  # Add explicit return at the end of the while loop

    return wrapped


structured_gemini_completion = with_structured_output(_completion_with_multi_fallback)
structured_azure_grok_completion = with_structured_output(azure_grok_acompletion)
structured_azure_completion = with_structured_output(azure_acompletion)


def get_backend_for_model(model: str) -> str:
    if model == LLMModel.AZURE_GROK_3:
        return "azure_grok"
    elif model.startswith("azure"):
        return "azure"
    elif model.startswith("vertex_ai"):
        return "vertex"
    else:
        raise ValueError(f"Unknown model prefix for model: {model}")


@observe(name="llm_completion", as_type="generation")
async def llm_completion(*, model: str, messages: list, **kwargs):
    # Ensure Langfuse is authenticated before any LLM operations
    _ensure_langfuse_authenticated()

    backend = get_backend_for_model(model)

    if backend == "vertex":
        result = await _completion_with_multi_fallback(model=model, messages=messages, **kwargs)
    elif backend == "azure_grok":
        result = await azure_grok_acompletion(model=model, messages=messages, **kwargs)
    elif backend == "azure":
        result = await azure_acompletion(model=model, messages=messages, **kwargs)
    else:
        raise ValueError(f"Unsupported backend: {backend}")

    return result


def structured_output_llm_completion_builder_func(response_format):
    async def wrapped(*, model: str, messages: list, **kwargs):
        # Ensure Langfuse is authenticated before any LLM operations
        _ensure_langfuse_authenticated()

        backend = get_backend_for_model(model)
        if backend == "vertex":
            return await structured_gemini_completion(
                messages=messages,
                response_format=response_format,
                model=model,
                **kwargs,
            )
        elif backend == "azure_grok":
            return await structured_azure_grok_completion(
                messages=messages,
                response_format=response_format,
                model=model,
                **kwargs,
            )
        elif backend == "azure":
            return await structured_azure_completion(
                messages=messages,
                response_format=response_format,
                model=model,
                **kwargs,
            )
        else:
            raise ValueError(f"Unsupported backend: {backend}")

    return wrapped
