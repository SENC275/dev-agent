"""Shared constraints for model-generated artifacts."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InvestigationArtifact(ArtifactModel):
    # These are distinct from repository observations and may be omitted.
    assumptions: list[Text] = Field(default_factory=list)
    unresolved_questions: list[Text] = Field(default_factory=list)
