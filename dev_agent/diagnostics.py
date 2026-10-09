"""Bounded, best-effort redaction for local provider failure diagnostics."""

import re

from dev_agent.models.agent import AgentResult


def redact(text: str) -> str:
    # Redact before truncation so long credentials cannot leak at the boundary.
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
                  "[REDACTED PRIVATE KEY]", text, flags=re.S)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s,;]+", r"\1 [REDACTED]", text)
    text = re.sub(r"(?i)([\w.-]*(?:token|secret|password|api[_-]?key|authorization)[\w.-]*"
                  r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
                  r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+|AKIA[A-Z0-9]{16})\b",
                  "[REDACTED]", text)
    text = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    return text[:4000] + ("\n[truncated]" if len(text) > 4000 else "")


def failure_details(result: AgentResult) -> dict[str, str]:
    message = result.error or "Provider failed without an error message."
    if result.timed_out:
        kind = "timeout"
    elif result.exit_code != 0:
        kind = "provider_execution"
    else:
        kind = "model_output"
    return {"kind": kind, "error": redact(message), "stderr": redact(result.stderr),
            "hint": {
                "timeout": "Check provider timeout and partial worktree changes before resuming.",
                "provider_execution": "Inspect provider error and stderr; check service/auth only "
                                      "when indicated by the error, then resume.",
                "model_output": "Inspect response format/schema failure before resuming.",
            }[kind]}
