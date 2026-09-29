"""The workflow depends on this interface, not on vendor commands."""

from abc import ABC, abstractmethod

from dev_agent.models.agent import AgentResult, AgentTask


class AgentProvider(ABC):
    @abstractmethod
    async def execute(self, task: AgentTask) -> AgentResult:
        """Execute a fresh task. Launch errors propagate; cancellation is preserved."""
        ...
