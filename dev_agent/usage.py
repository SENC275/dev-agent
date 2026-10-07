"""Local per-invocation telemetry; no prompts, source text or responses are stored here."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.usage import TokenUsage
from dev_agent.providers.base import AgentProvider


async def record_call(
    provider: AgentProvider,
    task: AgentTask,
    directory: Path,
    stage: str,
    provider_name: str,
    provider_type: str,
    model: str | None,
    configuration_sha256: str | None = None,
    context_info: dict[str, Any] | None = None,
) -> AgentResult:
    directory = directory.absolute()
    # Artifact directories are already selected by the workflow. Refuse symlink escapes.
    if any(p.is_symlink() for p in [directory, *directory.parents]):
        raise ValueError("Usage artifact directory must not contain symlinks.")
    folder = directory / "usage"
    if folder.is_symlink():
        raise ValueError("Usage artifact directory must not be a symlink.")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (uuid4().hex + ".json")
    record: dict[str, Any] = {
        "prompt_sha256": hashlib.sha256(task.prompt.encode()).hexdigest(),
        "version": 1,
        "id": path.stem,
        "stage": stage,
        "role": task.role,
        "provider": provider_name,
        "provider_type": provider_type,
        "configured_model": model,
        "configuration_sha256": configuration_sha256,
        "context": context_info or {"used": False},
        "working_directory": str(task.working_directory),
        "started_at": datetime.now(UTC).isoformat(),
        "status": "RUNNING",
        "duration_seconds": None,
        "usage": TokenUsage().model_dump(),
    }

    def save() -> None:
        temp = path.with_suffix(".tmp")
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
        temp.replace(path)

    save()
    start = monotonic()
    try:
        result = await provider.execute(task)
        usage = result.usage.model_copy()
        if result.timed_out:
            usage.complete = False
        record.update(
            status="SUCCEEDED" if result.success else "FAILED",
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            usage=usage.model_dump(),
        )
        return result
    except BaseException as exc:
        record.update(
            status="INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "ERROR",
            error_type=type(exc).__name__,
        )
        raise
    finally:
        record.update(
            duration_seconds=monotonic() - start, finished_at=datetime.now(UTC).isoformat()
        )
        save()


def usage_report(root: Path, ticket: str | None = None) -> dict[str, Any]:
    base = root / ".dev-agent"
    records = []
    for path in sorted(base.rglob("usage/*.json")):
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ValueError("Refusing symlinked usage records.")
        relative = path.relative_to(base)
        parts = relative.parts
        if ticket and not (
            (parts[0] == "runs" and len(parts) > 1 and parts[1] == ticket)
            or (parts[0] == "tickets" and len(parts) > 1 and parts[1] == ticket)
        ):
            continue
        data = json.loads(path.read_text())
        data["artifact"] = str(relative)
        records.append(data)
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = (
            record["stage"],
            record["role"],
            record["provider"],
            record.get("configured_model") or "default",
        )
        groups.setdefault(key, []).append(record)
    summaries = []
    for key, rows in sorted(groups.items()):
        row: dict[str, Any] = dict(zip(("stage", "role", "provider", "model"), key, strict=True))
        row.update(
            calls=len(rows),
            failed_calls=sum(r["status"] in {"FAILED", "ERROR", "INTERRUPTED"} for r in rows),
            running_calls=sum(r["status"] == "RUNNING" for r in rows),
            duration_seconds=sum(r.get("duration_seconds") or 0 for r in rows),
            complete_calls=sum(r["usage"].get("complete", False) for r in rows),
        )
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            values = [r["usage"].get(field) for r in rows]
            known = [v for v in values if v is not None]
            row[field] = sum(known) if known else None
            row[field + "_known_calls"] = len(known)
        summaries.append(row)
    return {
        "schema_version": 1,
        "ticket": ticket,
        "recorded_calls": len(records),
        "groups": summaries,
        "records": records,
        "note": "Known subtotals only; missing/partial usage is not zero. "
        "Cache tokens are included in input, not additive. "
        "Durations sum agent calls, not workflow wall time. "
        "Historical calls before instrumentation are not included.",
    }
