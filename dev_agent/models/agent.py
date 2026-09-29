"""Provider-neutral task and result boundaries."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    working_directory: Path
    read_only: bool = Field(default=True, strict=True)
    output_schema: dict[str, Any] | None = None


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool
    output: str
    exit_code: int
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None
