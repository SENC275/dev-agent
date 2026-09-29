"""Codex non-interactive adapter. Each call starts a fresh ephemeral session."""

from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ProviderConfig
from dev_agent.process import ProcessResult, run_process
from dev_agent.providers.base import AgentProvider


class ProcessRunner(Protocol):
    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        stdin: str | None = None,
        timeout: float = 60.0,
    ) -> ProcessResult: ...


class CodexCLIProvider(AgentProvider):
    def __init__(self, config: ProviderConfig, *, runner: ProcessRunner = run_process) -> None:
        self.config = config
        self.runner = runner

    async def execute(self, task: AgentTask) -> AgentResult:
        with TemporaryDirectory(prefix="dev-agent-codex-") as directory:
            output_path = Path(directory) / "response.txt"
            command = [
                "codex",
                "--ask-for-approval",
                "never",
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only" if task.read_only else "workspace-write",
                "--color",
                "never",
                "--output-last-message",
                str(output_path),
            ]
            if self.config.model is not None:
                command.extend(["--model", self.config.model])
            command.append("-")
            result = await self.runner(
                command,
                cwd=task.working_directory,
                stdin=task.prompt,
                timeout=self.config.timeout_seconds,
            )
            # stdout can contain progress/tool output, never treat it as the answer.
            try:
                output = output_path.read_text(encoding="utf-8")
                output_error = None if output.strip() else "Codex returned an empty final response."
            except FileNotFoundError:
                output = ""
                output_error = "Codex did not produce a final response."
            except UnicodeError:
                output = ""
                output_error = "Codex final response is not valid UTF-8."
            error = output_error
            if result.timed_out:
                error = "Codex execution timed out."
            elif result.exit_code != 0:
                error = f"Codex exited with code {result.exit_code}; inspect stderr."
            return AgentResult(
                success=error is None,
                output=output,
                exit_code=result.exit_code,
                duration_seconds=result.duration_seconds,
                stderr=result.stderr,
                timed_out=result.timed_out,
                error=error,
            )
