"""Every review finding must receive an explicit, evidence-backed disposition."""

from typing import Literal

from pydantic import Field

from dev_agent.models.artifact import ArtifactModel, Text


class FindingDecision(ArtifactModel):
    finding_index: int = Field(ge=0)
    disposition: Literal["fixed", "rejected", "deferred"]
    explanation: Text
    evidence: list[Text] = Field(min_length=1)


class FixReport(ArtifactModel):
    decisions: list[FindingDecision]
    summary: Text
