"""Deterministic in-memory provider for tests; no subprocess or network access."""

from collections.abc import Mapping

from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.providers.base import AgentProvider


class FakeProvider(AgentProvider):
    def __init__(self, responses: Mapping[str, AgentResult]) -> None:
        self.responses = dict(responses)
        self.tasks: list[AgentTask] = []

    async def execute(self, task: AgentTask) -> AgentResult:
        if task.role not in self.responses:
            raise ValueError(f"No fake response configured for role '{task.role}'")
        self.tasks.append(task.model_copy(deep=True))
        return self.responses[task.role].model_copy(deep=True)
