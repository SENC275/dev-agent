"""Resolve role configuration using provider factories, without vendor branching."""

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path

from dev_agent.context import prepare
from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ProjectConfig, ProviderConfig
from dev_agent.project_context import prepare as prepare_project
from dev_agent.providers.base import AgentProvider
from dev_agent.providers.claude_cli import ClaudeCLIProvider
from dev_agent.providers.codex_cli import CodexCLIProvider
from dev_agent.providers.ollama import OllamaProvider
from dev_agent.usage import record_call


class ProviderRegistry:
    def __init__(
        self,
        config: ProjectConfig,
        factories: Mapping[str, Callable[[ProviderConfig], AgentProvider]] | None = None,
    ) -> None:
        self.config = config
        self.factories = (
            dict(factories)
            if factories is not None
            else {
                "codex_cli": CodexCLIProvider,
                "ollama": OllamaProvider,
                "claude_cli": ClaudeCLIProvider,
            }
        )

    def resolve(self, role: str) -> AgentProvider:
        if role not in self.config.roles:
            raise ValueError(f"Unknown role '{role}'")
        name = self.config.roles[role].provider
        provider = self.config.providers[name]
        if provider.type not in self.factories:
            raise ValueError(f"No factory registered for provider type '{provider.type}'")
        return self.factories[provider.type](provider)

    async def execute(self, stage: str, directory: Path, task: AgentTask) -> AgentResult:
        if stage == "implement" and self.config.knowledge.enabled:
            context_info = {"used": False, "reason": "investigation_already_in_knowledge_bundle"}
        else:
            task, context_info = prepare(task, directory, stage, self.config.context)
        task, project_info = await prepare_project(task, stage, self.config.project_context)
        context_info["project"] = project_info
        name = self.config.roles[task.role].provider
        config = self.config.providers[name]
        return await record_call(
            self.resolve(task.role),
            task,
            directory,
            stage,
            name,
            config.type,
            config.model,
            hashlib.sha256(self.config.model_dump_json().encode()).hexdigest(),
            context_info,
        )
