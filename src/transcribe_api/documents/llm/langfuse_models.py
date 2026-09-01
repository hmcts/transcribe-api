from typing import Any

from pydantic import BaseModel


class LangfuseTraceRequest(BaseModel):
    """Request model for submitting Langfuse traces."""

    trace_id: str
    name: str
    metadata: dict[str, Any] | None = None
    input_data: dict[str, Any] | str | None = None
    output_data: dict[str, Any] | str | None = None


class LangfuseScoreRequest(BaseModel):
    """Request model for submitting Langfuse scores."""

    trace_id: str
    name: str
    value: float
    comment: str | None = None
