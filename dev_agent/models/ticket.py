"""Structured draft; unresolved product choices remain explicit."""

from pydantic import Field

from dev_agent.models.artifact import ArtifactModel, Text


class TicketDraft(ArtifactModel):
    title: Text
    goal: Text
    current_behavior: Text
    acceptance_criteria: list[Text] = Field(min_length=1)
    expected_files: list[Text]
    out_of_scope: list[Text]
    assumptions: list[Text]
    open_questions: list[Text]
