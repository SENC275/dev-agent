"""Fresh Claude Code CLI sessions with explicitly restricted file tools."""

import json

from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ProviderConfig
from dev_agent.process import run_process
from dev_agent.providers.base import AgentProvider
from dev_agent.providers.codex_cli import ProcessRunner


class ClaudeCLIProvider(AgentProvider):
    def __init__(self, config: ProviderConfig, *, runner: ProcessRunner = run_process):
        self.config = config
        self.runner = runner

    async def execute(self, task: AgentTask) -> AgentResult:
        tools = ["Read", "Glob", "Grep"]
        allowed = list(tools)
        if not task.read_only:
            tools += ["Edit", "Write"]
            allowed += ["Edit(./**)", "Write(./**)"]
        protected = [".git", ".dev-agent", ".env", ".env.*", "**/.env", "**/.env.*"]
        denied = [
            f"{tool}(./{path}{suffix})"
            for tool in ("Read", "Edit", "Write")
            for path in protected
            for suffix in ("", "/**")
        ]
        command = [
            "claude",
            "--print",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--restricted",
            "--safe-mode",
            "--permission-mode",
            "dontAsk",
            "--tools",
            ",".join(tools),
            "--allowedTools",
            ",".join(allowed),
            "--disallowedTools",
            "Bash,Agent,Task,WebFetch,WebSearch,mcp__*",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--setting-sources",
            "",
            "--settings",
            json.dumps(
                {
                    "disableAllHooks": True,
                    "permissions": {"deny": denied},
                }
            ),
            "--max-turns",
            str(self.config.max_steps),
            "--append-system-prompt",
            "Work only inside the current repository. Read applicable AGENTS.md and CLAUDE.md "
            "before work, including nested instructions. Preserve unrelated files and content. "
            "No shell, network, subagent or validation tools are provided. "
            "Do not modify .git or .dev-agent, read credentials, or claim tests ran. "
            "Return only the requested final answer; JSON answers must have no Markdown fences.",
        ]
        if task.output_schema is not None:
            command += ["--json-schema", json.dumps(task.output_schema)]
        if self.config.model:
            command += ["--model", self.config.model]
        process = await self.runner(
            command,
            cwd=task.working_directory,
            stdin=task.prompt,
            timeout=self.config.timeout_seconds,
        )
        output = ""
        error = None
        if process.timed_out:
            error = "Claude execution timed out; inspect partial worktree changes."
        elif process.exit_code != 0:
            error = (
                f"Claude exited with code {process.exit_code}; check claude auth status, "
                "CLI version/flags and stderr."
            )
        else:
            try:
                payload = json.loads(process.stdout)
                if not isinstance(payload, dict) or payload.get("type") != "result":
                    raise ValueError("Missing final result envelope.")
                if payload.get("is_error") is not False or payload.get("subtype") != "success":
                    raise ValueError("Claude reported an unsuccessful run or exhausted turn limit.")
                if payload.get("permission_denials"):
                    raise ValueError(
                        "Claude reported denied tool calls; inspect the requested work."
                    )
                result: object
                if task.output_schema is not None:
                    structured = payload.get("structured_output")
                    if not isinstance(structured, dict):
                        raise ValueError("Claude did not return the requested structured output.")
                    result = json.dumps(structured)
                else:
                    result = payload.get("result")
                if not isinstance(result, str) or not result.strip():
                    raise ValueError("Claude returned an empty or invalid final response.")
                output = result.strip()
            except ValueError as exc:
                error = f"Claude response failed: {exc}"
        return AgentResult(
            success=error is None,
            output=output,
            error=error,
            exit_code=process.exit_code,
            duration_seconds=process.duration_seconds,
            stderr=process.stderr,
            timed_out=process.timed_out,
        )
