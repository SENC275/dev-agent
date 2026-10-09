"""Read-only human review overview; no provider calls or workflow state changes."""

import json
import shlex
from pathlib import Path
from typing import Any

from dev_agent.context import safe
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import git
from dev_agent.models.finding import Review
from dev_agent.usage import usage_report
from dev_agent.validation.runner import ValidationRun
from dev_agent.workflow.merging import preview_merge
from dev_agent.workflow.persistence import Journal
from dev_agent.workflow.planning import approval_is_current


def read(path: Path) -> dict[str, Any]:
    if not safe(path) or path.stat().st_size > 4_000_000:
        raise ValueError("Unsafe or oversized summary artifact")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Invalid summary artifact")
    return data


async def summarize(journal: Journal, ticket: str) -> dict[str, Any]:
    run = journal.get(ticket)
    report: dict[str, Any] = {
        "ticket": ticket, "state": run["state"], "stage": run["current_step"],
        "worktree": run["worktree_path"], "plan": run["plan_path"],
        "files": [], "validation": None, "review": None, "usage": None,
        "warnings": [], "merge_ready": False, "commands": [],
    }
    plan = Path(run["plan_path"]) if run["plan_path"] else None
    snapshot = None
    approval_current = False
    validation = None
    base = None
    target = Path(run["worktree_path"]) if run["worktree_path"] else None
    if plan and target:
        try:
            record = read(plan / "worktree.json")
            if record.get("path") != str(target):
                raise ValueError("Worktree record does not match journal")
            base = record["base_commit"]
            snapshot = await capture_snapshot(target, base)
            raw = await git(target, "diff", "--name-status", "--no-renames", "-z", base, "--")
            parts = raw.split("\0")
            report["files"] = [{"status": parts[i], "path": parts[i + 1]}
                               for i in range(0, len(parts) - 1, 2)]
            untracked = await git(target, "ls-files", "--others", "--exclude-standard", "-z")
            report["files"].extend({"status": "?", "path": n}
                                   for n in untracked.split("\0") if n)
            approval_current = await approval_is_current(plan)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report["warnings"].append(f"Current change check unavailable: {exc}")
    if plan:
        try:
            validation = ValidationRun.model_validate(read(plan / "validation.json"))
            approval = read(plan / "approval.json")
            current = bool(snapshot and validation.snapshot_sha256 == snapshot.fingerprint
                           and validation.worktree == target and validation.base_commit == base
                           and validation.plan_sha256 == approval.get("plan_sha256")
                           and approval_current)
            report["validation"] = {
                "status": validation.status, "current": current,
                "commands": [{"name": c.name, "command": c.command, "status": c.status,
                              "exit_code": c.exit_code, "timed_out": c.timed_out}
                             for c in validation.commands],
                "artifact": str(plan / "validation.json"),
            }
        except (OSError, ValueError, KeyError, TypeError):
            report["warnings"].append("Validation is missing or unreadable; no pass inferred.")
        try:
            metadata = read(plan / "review-status.json")
            findings = Review.model_validate(read(plan / "review.json"))
            current = bool(snapshot and validation and report["validation"]
                           and report["validation"]["current"] and validation.success
                           and metadata.get("status") == "REVIEWED"
                           and metadata.get("snapshot_sha256") == snapshot.fingerprint
                           and metadata.get("plan_sha256") == validation.plan_sha256)
            archive = Path(str(metadata.get("review_directory", "")))
            if not archive.resolve().is_relative_to((plan / "reviews").resolve()):
                current = False
            elif current:
                archived = Review.model_validate(read(archive / "review.json"))
                current = archived == findings
            report["review"] = {"status": metadata.get("status"), "current": current,
                                "findings": findings.model_dump()["findings"],
                                "artifact": str(plan / "review.json")}
        except (OSError, ValueError, KeyError, TypeError):
            report["warnings"].append("Review is missing or unreadable; no zero-findings inferred.")
    try:
        records = usage_report(journal.repository, ticket)["records"]
        metrics: dict[str, Any] = {"calls": len(records)}
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            known = [x["usage"].get(key) for x in records if x["usage"].get(key) is not None]
            metrics[key] = sum(known) if known else None
            metrics[key + "_complete"] = bool(records) and len(known) == len(records) and all(
                x["usage"].get("complete") for x in records
            )
        report["usage"] = metrics
    except (OSError, ValueError, TypeError, KeyError):
        report["warnings"].append("Usage records are unreadable; totals unavailable.")
    quoted = shlex.quote(ticket)
    if target and base:
        root = shlex.quote(str(target))
        report["commands"].extend([f"git -C {root} status --short",
                                   f"git -C {root} diff {shlex.quote(base)} --"])
        if any(f["status"] == "?" for f in report["files"]):
            report["warnings"].append(
                "Git diff omits untracked files; inspect the listed new files."
            )
    if run["state"] == "READY_FOR_HUMAN_REVIEW":
        try:
            await preview_merge(journal, ticket)
            report["merge_ready"] = True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report["warnings"].append(f"Merge checks blocked: {exc}")
        report["commands"].append(f"dev-agent revise {quoted} --file /path/to/feedback.md")
        report["commands"].append(f"dev-agent merge {quoted} --dry-run")
        if report["merge_ready"]:
            report["commands"].append(f"dev-agent merge {quoted}")
    elif run["state"] not in {"MERGED", "MERGING", "MERGE_FAILED"}:
        report["commands"].append(f"dev-agent resume {quoted}")
    report["commands"].append(f"dev-agent status {quoted}")
    return report


def render(data: dict[str, Any]) -> str:
    lines = [f"{data['ticket']}: {data['state']} (stage: {data['stage']})",
             f"Worktree: {data['worktree'] or 'not created'}", "", "Changed files:"]
    lines.extend(f"  {f['status']} {f['path']}" for f in data["files"][:30])
    if len(data["files"]) > 30:
        lines.append(f"  … {len(data['files']) - 30} more; use --json or git status")
    if not data["files"]:
        lines.append("  No changes listed (see warnings if unavailable).")
    validation = data["validation"]
    lines.append("\nValidation: " + (
        f"{validation['status']} ({'current' if validation['current'] else 'STALE/unverified'})"
        if validation else "unavailable"))
    if validation:
        lines.extend(f"  {c['name']}: {c['status']} (exit={c['exit_code']}) — {c['command']}"
                     for c in validation["commands"])
    review = data["review"]
    lines.append("\nReview: " + (
        f"{review['status']} ({'current' if review['current'] else 'STALE/unverified'}), "
        f"{len(review['findings'])} recorded findings" if review else "unavailable"))
    if review:
        lines.extend(f"  [{f['severity']}] {f['file']}:{f['line']} — {f['scenario']} "
                     f"→ {f['recommendation']}" for f in review["findings"][:10])
        if len(review["findings"]) > 10:
            lines.append("  More findings in --json and review.json")
    usage = data["usage"]
    if usage:
        values = []
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            value = usage[name]
            text = "unknown" if value is None else f"{value:,}"
            if value is not None and not usage[name + "_complete"]:
                text += " (known subtotal)"
            values.append(f"{name}={text}")
        lines.append(f"\nUsage: {usage['calls']} recorded calls; " + "; ".join(values))
        lines.append("  Input includes cache. Older unmetered calls are not included.")
    lines.append("\nMerge checks: " + ("passed; human acceptance required" if data["merge_ready"]
                                     else "not cleared"))
    lines.extend(f"Note: {warning}" for warning in data["warnings"])
    lines.append("\nNext commands (run from the source repository):")
    lines.extend("  " + command for command in data["commands"])
    return "\n".join(lines)
