"""Advisory review findings; severity never grants approval."""

from typing import Literal

from pydantic import Field

from dev_agent.models.artifact import ArtifactModel, Text


class Finding(ArtifactModel):
    severity: Literal["critical", "high", "medium", "low"]
    category: Text
    file: Text
    line: int = Field(gt=0)
    scenario: Text
    impact: Text
    recommendation: Text


class Review(ArtifactModel):
    findings: list[Finding]
