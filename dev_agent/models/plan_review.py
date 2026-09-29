"""Structured decision from an independent, read-only plan reviewer."""

from typing import Literal

from pydantic import model_validator

from dev_agent.models.artifact import ArtifactModel, Text


class PlanReview(ArtifactModel):
    decision: Literal["approve", "request_changes"]
    summary: Text
    issues: list[Text]

    @model_validator(mode="after")
    def consistent_decision(self) -> "PlanReview":
        if self.decision == "approve" and self.issues:
            raise ValueError("Approval cannot contain unresolved issues.")
        if self.decision == "request_changes" and not self.issues:
            raise ValueError("Requested changes must explain at least one issue.")
        return self
