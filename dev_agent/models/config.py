"""Configuration contract for engineering workflows."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROLES = (
    "explorer",
    "pattern_researcher",
    "test_researcher",
    "planner",
    "implementer",
    "reviewer",
    "fixer",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProviderConfig(StrictModel):
    type: Literal["codex_cli", "ollama", "claude_cli"] = "codex_cli"
    model: str | None = Field(default=None, min_length=1)
    timeout_seconds: float = Field(default=600.0, gt=0, allow_inf_nan=False)

    base_url: str = "http://127.0.0.1:11434"
    num_ctx: int = Field(default=8192, ge=2048, le=131072)
    max_steps: int = Field(default=24, ge=1, le=100)
    max_output_tokens: int = Field(default=2048, ge=128, le=16384)

    @model_validator(mode="after")
    def validate_ollama(self) -> "ProviderConfig":
        if self.type == "ollama":
            url = urlsplit(self.base_url)
            _ = url.port  # Validate malformed/out-of-range ports early.
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
            ):
                raise ValueError("Ollama base_url must be an HTTP(S) origin without credentials.")
            if not self.model or not self.model.strip() or self.max_output_tokens >= self.num_ctx:
                raise ValueError("Ollama requires a model and output limit smaller than num_ctx.")
        return self


class RoleConfig(StrictModel):
    provider: str = Field(default="codex", min_length=1)


class CommandsConfig(StrictModel):
    test: str = Field(default="pytest", min_length=1)
    lint: str = Field(default="ruff check .", min_length=1)
    typecheck: str = Field(default="mypy src", min_length=1)


class ValidationConfig(StrictModel):
    timeout_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)


class GitConfig(StrictModel):
    branch_pattern: str = "codex/{ticket}"
    worktree_directory: str = "../worktrees/{ticket}"


class GatesConfig(StrictModel):
    plan_review: Literal["human", "agent"] = "human"
    approve_plan: Literal[True] = True
    approve_final: Literal[True] = True


class LimitsConfig(StrictModel):
    max_fix_cycles: int = Field(default=2, ge=0)
    max_plan_revisions: int = Field(default=2, ge=0, le=10)


class KnowledgeConfig(StrictModel):
    enabled: bool = False


class ContextConfig(StrictModel):
    enabled: bool = True
    investigation_mode: Literal["parallel", "explorer_first", "single_pass"] = "parallel"
    max_characters: int = Field(default=4000, ge=2000, le=32000)


class ProjectContextConfig(StrictModel):
    enabled: bool = False
    max_characters: int = Field(default=3000, ge=1500, le=12000)


class ProjectConfig(StrictModel):
    project: str | None = None
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {"codex": ProviderConfig()}
    )
    roles: dict[str, RoleConfig] = Field(
        default_factory=lambda: {role: RoleConfig() for role in ROLES}
    )
    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    project_context: ProjectContextConfig = Field(default_factory=ProjectContextConfig)

    @model_validator(mode="after")
    def validate_roles(self) -> "ProjectConfig":
        missing = set(ROLES) - self.roles.keys()
        unknown = self.roles.keys() - set(ROLES)
        if missing or unknown:
            raise ValueError(f"Invalid roles: missing={sorted(missing)}, unknown={sorted(unknown)}")
        for role, config in self.roles.items():
            if config.provider not in self.providers:
                raise ValueError(f"Role '{role}' references undefined provider '{config.provider}'")
        return self
