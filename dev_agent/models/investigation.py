"""Structured exploration, repository patterns, and test analysis."""

from pydantic import Field

from dev_agent.models.artifact import ArtifactModel, InvestigationArtifact, Text


class Exploration(InvestigationArtifact):
    entry_points: list[Text]
    call_chain: list[Text]
    database_writes: list[Text]
    external_calls: list[Text]
    important_files: list[Text]
    risks: list[Text]


class Pattern(ArtifactModel):
    name: Text
    files: list[Text] = Field(min_length=1)
    description: Text


class PatternAnalysis(InvestigationArtifact):
    patterns: list[Pattern]


class TestAnalysis(InvestigationArtifact):
    existing_tests: list[Text]
    fixtures: list[Text]
    recommended_tests: list[Text]
