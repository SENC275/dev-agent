"""Resolve role configuration using provider factories, without vendor branching."""

from collections.abc import Callable, Mapping

from dev_agent.models.config import ProjectConfig, ProviderConfig
from dev_agent.providers.base import AgentProvider
from dev_agent.providers.claude_cli import ClaudeCLIProvider
from dev_agent.providers.codex_cli import CodexCLIProvider
from dev_agent.providers.ollama import OllamaProvider


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
