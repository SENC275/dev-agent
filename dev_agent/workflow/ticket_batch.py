"""Sequential local draft batches with provenance and conservative repeat handling."""

import fcntl
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from dev_agent.context import safe
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import git
from dev_agent.local import protect_local_files
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.ticket import generate_ticket


def fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sources(folder: Path) -> dict[str, Any]:
    if not safe(folder) or not folder.is_dir():
        raise ValueError("Input folder must be a real directory, without symlinks.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", folder.name):
        raise ValueError("Folder name must be a valid ticket ID (1–100 ASCII letters/digits/_/-).")
    primary = folder / "description.md"
    if not primary.is_file():
        raise ValueError("Missing description.md.")
    files = sorted(
        (
            p
            for p in folder.iterdir()
            if not p.name.startswith(".") and p.suffix.lower() in {".md", ".txt"}
        ),
        key=lambda p: (p.name != "description.md", p.name),
    )
    if len(files) > 30:
        raise ValueError("At most 30 Markdown/text files per ticket.")
    parts, references = [], []
    for path in files:
        if not safe(path) or not path.is_file() or path.stat().st_size > 32_000:
            raise ValueError(f"Unsafe or oversized source: {path.name}")
        raw = path.read_bytes()
        content = raw.decode("utf-8")
        if "\0" in content or (path == primary and not content.strip()):
            raise ValueError(f"Empty description or binary source: {path.name}")
        parts.append(f"## Source: {path.name}\n\n{content}")
        references.append({"file": path.name, "sha256": fingerprint(raw)})
    description = "\n\n".join(parts)
    if len(description.encode()) > 32_000:
        raise ValueError("Combined description exceeds 32 KB, including source headings.")
    return {"folder": str(folder), "files": references, "description": description}


def write_record(path: Path, data: dict[str, Any]) -> None:
    if not safe(path):
        raise ValueError("Unsafe batch record path.")
    temporary = path.with_name("." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def receipt_matches(path: Path, expected: dict[str, Any], output: Path) -> bool:
    if not safe(path) or not path.is_file() or path.stat().st_size > 128_000:
        return False
    if not safe(output) or not output.is_file() or output.stat().st_size > 2_000_000:
        return False
    try:
        data = json.loads(path.read_text())
        return (
            isinstance(data, dict)
            and data.get("schema_version") == 1
            and data.get("inputs") == expected
            and data.get("draft_sha256") == fingerprint(output.read_bytes())
        )
    except (OSError, ValueError):
        return False


async def generate_batch(
    repository: Path,
    folder: Path,
    registry: ProviderRegistry,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    # Check symlinks before resolve; never traverse an input link silently.
    folder = folder.absolute()
    if not safe(folder) or not folder.is_dir():
        raise ValueError("Batch folder must be a real directory without symlinks.")
    children = sorted(
        p for p in folder.iterdir() if not p.name.startswith(".") and (p.is_dir() or p.is_symlink())
    )
    if not children or len(children) > 100:
        raise ValueError("Batch folder must contain 1–100 ticket subfolders.")
    repository = repository.resolve(strict=True)
    protect_local_files(repository)
    root = repository / ".dev-agent" / "ticket-batches"
    receipts = root / "receipts"
    if not safe(receipts) or not safe(root / "batch.lock"):
        raise ValueError("Unsafe batch output directory.")
    receipts.mkdir(parents=True, exist_ok=True)
    # A batch lock prevents competing batches from producing inconsistent receipts.
    # Single-ticket writers remain protected by generate_ticket's exclusive create.
    with (root / "batch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another ticket batch is running in this repository.") from None
        report: dict[str, Any] = {
            "schema_version": 1,
            "batch_id": uuid4().hex,
            "folder": str(folder),
            "started_at": datetime.now(UTC).isoformat(),
            "items": [],
        }
        report_path = root / (report["batch_id"] + ".json")
        report["report_path"] = str(report_path)
        initial_base = await git(repository, "rev-parse", "HEAD")
        initial_snapshot = await capture_snapshot(repository, initial_base)
        config_hash = fingerprint(registry.config.model_dump_json().encode())
        counts: dict[str, int] = {}
        for child in children:
            counts[child.name.casefold()] = counts.get(child.name.casefold(), 0) + 1
        # Read and validate the whole input inventory before spending any model calls.
        prepared: list[tuple[Path, dict[str, Any] | None, str | None]] = []
        for child in children:
            try:
                if counts[child.name.casefold()] > 1:
                    raise ValueError("Case-insensitive duplicate ticket IDs.")
                prepared.append((child, sources(child), None))
            except (OSError, ValueError) as exc:
                prepared.append((child, None, str(exc)))
        write_record(report_path, report)
        for index, (child, source, error) in enumerate(prepared, 1):
            started = monotonic()
            item: dict[str, Any] = {"ticket_id": child.name, "status": "RUNNING"}
            report["items"].append(item)
            if progress:
                progress(f"{index}/{len(prepared)} · {child.name}")
            write_record(report_path, report)
            try:
                if source is None:
                    item.update(status="INVALID", error=error)
                    continue
                output = repository / ".dev-agent" / "tickets" / (child.name + ".md")
                receipt = receipts / (child.name + ".json")
                if sources(child) != source:
                    raise ValueError("Input changed after preflight; run batch again.")
                base = await git(repository, "rev-parse", "HEAD")
                snapshot = await capture_snapshot(repository, base)
                if (
                    base != initial_base
                    or snapshot.content_fingerprint != initial_snapshot.content_fingerprint
                ):
                    raise ValueError("Repository changed during batch; inspect before retrying.")
                inputs = {
                    "source_folder": source["folder"],
                    "files": source["files"],
                    "config_sha256": config_hash,
                    "base_commit": base,
                    "source_snapshot_sha256": snapshot.content_fingerprint,
                }
                item["inputs"] = inputs
                if not safe(output) or output.exists() or receipt.exists() or receipt.is_symlink():
                    if receipt_matches(receipt, inputs, output):
                        item.update(status="SKIPPED", output=str(output))
                    else:
                        item.update(
                            status="CONFLICT",
                            error=(
                                "Existing draft/receipt differs or is unverified. "
                                "Nothing overwritten; "
                                "review it manually or use a new folder ID."
                            ),
                        )
                    continue
                output = await generate_ticket(
                    repository, child.name, source["description"], registry
                )
                if sources(child) != source:
                    raise ValueError(
                        "Input changed during generation; saved draft needs manual review."
                    )
                if (
                    await git(repository, "rev-parse", "HEAD") != base
                    or (await capture_snapshot(repository, base)).content_fingerprint
                    != snapshot.content_fingerprint
                ):
                    raise ValueError("Repository changed; saved draft needs manual review.")
                write_record(
                    receipt,
                    {
                        "schema_version": 1,
                        "inputs": inputs,
                        "batch_id": report["batch_id"],
                        "output": str(output),
                        "draft_sha256": fingerprint(output.read_bytes()),
                    },
                )
                item.update(status="GENERATED", output=str(output))
            except (OSError, ValueError) as exc:
                item.update(status="FAILED", error=str(exc))
            except Exception as exc:
                # Isolate provider failures, but let cancellation/interrupts propagate.
                # Arbitrary exception messages may include provider request data.
                item.update(
                    status="FAILED",
                    error=(
                        f"Unexpected generation failure ({type(exc).__name__}); "
                        "inspect local usage."
                    ),
                )
            finally:
                item["duration_seconds"] = round(monotonic() - started, 3)
                write_record(report_path, report)
                if progress:
                    progress(
                        f"{index}/{len(prepared)} · {child.name} · {item['status']} "
                        f"({item['duration_seconds']:.1f}s)"
                    )
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_record(report_path, report)
        return report
