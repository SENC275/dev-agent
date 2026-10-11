"""Explicit local collection of reconstructed patch candidates, never automatic training."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from dev_agent.context import safe
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.review_summary import summarize
from dev_agent.usage import usage_report
from dev_agent.workflow.persistence import Journal

MAX_FILE = 2_000_000
MAX_BUNDLE = 20_000_000


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(name: str) -> bool:
    p = Path(name)
    return (
        bool(name)
        and not p.is_absolute()
        and not any(
            part.lower() in {"..", ".", ".git", ".dev-agent", ".aws", ".ssh"}
            or part.lower().startswith(".env")
            or part.lower() in {"credentials", "secrets"}
            for part in p.parts
        )
        and p.suffix.lower() not in {".pem", ".key", ".tfstate"}
        and p.name.lower() != ".dev-agent.yaml"
    )


def text(data: bytes) -> str:
    if len(data) > MAX_FILE or b"\0" in data:
        raise ValueError("oversized_or_binary")
    value = data.decode("utf-8")
    # Conservative first version: quarantine, never rewrite executable code to redact it.
    if re.search(
        r"-----BEGIN .*PRIVATE KEY|\b(?:sk-[\w-]{8,}|gh[pousr]_[\w]+|"
        r"github_pat_[\w]+|AKIA[A-Z0-9]{16})|"
        r"(?i:authorization\s*[:=]|bearer\s+|"
        r"(?:password|secret|api[_-]?key|access[_-]?token)[\"']?\s*[:=])",
        value,
    ):
        raise ValueError("possible_secret")
    return value


def git(repo: Path, *args: str, input_data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false", *args],
            cwd=repo,
            input=input_data,
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Corpus Git operation timed out") from exc
    if result.returncode:
        raise ValueError(f"Git {args[0]} failed (exit {result.returncode})")
    return result.stdout


def tree(repo: Path, revision: str) -> dict[str, tuple[str, str]]:
    result = {}
    for entry in git(repo, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not entry:
            continue
        info, raw_name = entry.split(b"\t", 1)
        mode, _, blob = info.decode().split()
        result[raw_name.decode()] = (mode, blob)
    return result


def read_artifact(path: Path) -> str:
    if not safe(path) or not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError("missing_or_unsafe")
    return text(path.read_bytes())


def materialize(root: Path, files: dict[str, Any]) -> None:
    for name, entry in files.items():
        if not safe_name(name) or entry["mode"] not in {"100644", "100755"}:
            raise ValueError("Invalid captured path or mode")
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(entry["content"], encoding="utf-8", newline="")
        p.chmod(0o755 if entry["mode"] == "100755" else 0o644)


def verify_patch(before: dict[str, Any], after: dict[str, Any], patch: str | None = None) -> str:
    """Rebuild changed files in an isolated temporary repository, with no source writes."""
    with TemporaryDirectory(prefix="dev-agent-corpus-") as temporary:
        root = Path(temporary)
        git(root, "init", "-q")
        # Captured .gitattributes must not activate filters or transformations.
        git(root, "config", "core.attributesFile", os.devnull)
        (root / ".git/info/attributes").write_text(
            "* -text -filter -ident -working-tree-encoding\n"
        )
        materialize(root, before)
        git(root, "add", "--all", "--force")
        old = git(root, "write-tree").decode().strip()
        for name in before:
            (root / name).unlink()
        # Remove empty directories to permit file/directory replacement.
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and ".git" not in path.relative_to(root).parts:
                try:
                    path.rmdir()
                except OSError:
                    pass
        materialize(root, after)
        git(root, "add", "--all", "--force")
        new = git(root, "write-tree").decode().strip()
        generated = git(
            root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary", old, new
        ).decode()
        if patch is None:
            patch = generated
        git(root, "read-tree", "--reset", "-u", old)
        if patch:
            git(
                root,
                "apply",
                "--check",
                "--index",
                "--whitespace=nowarn",
                input_data=patch.encode(),
            )
            git(root, "apply", "--index", "--whitespace=nowarn", input_data=patch.encode())
        if git(root, "write-tree").decode().strip() != new:
            raise ValueError("Patch does not reconstruct the captured result")
        for name, entry in after.items():
            if (root / name).read_bytes() != entry["content"].encode():
                raise ValueError("Reconstructed file contents differ")
        return patch


async def collect(journal: Journal, ticket: str) -> Path:
    with journal.lock(ticket):
        run = journal.get(ticket)
        gaps: list[str] = []
        payload: dict[str, Any] = {
            "schema_version": 1,
            "ticket_id": ticket,
            "run_id": run["run_id"],
            "provenance": "reconstructed",
            "task_type": "patch_generation",
            "state_at_collection": run["state"],
            "before": {},
            "after": {},
            "evidence": {},
            "input": {},
            "patch": None,
            "source": {},
            "quality": {
                "validation": "unknown",
                "review": "unknown",
                "human_acceptance": "unknown",
                "training_eligible": False,
            },
            "gaps": gaps,
        }
        try:
            payload["input"]["ticket"] = text((run["ticket"] or "").encode())
        except ValueError:
            gaps.append("ticket_withheld")
        plan = Path(run["plan_path"]) if run["plan_path"] else None
        inv = Path(run["investigation_path"]) if run["investigation_path"] else None
        evidence_sources = {}
        if inv:
            evidence_sources.update(
                {
                    name: inv / name
                    for name in (
                        "exploration.json",
                        "patterns.json",
                        "tests.json",
                        "investigation.json",
                    )
                }
            )
        if plan:
            evidence_sources.update(
                {
                    name: plan / name
                    for name in (
                        "plan.md",
                        "approval.json",
                        "validation.json",
                        "review.json",
                        "review-status.json",
                        "implementation.json",
                    )
                }
            )
        for name, path in evidence_sources.items():
            try:
                content = read_artifact(path)
                if name == "plan.md":
                    payload["input"]["plan"] = content
                else:
                    payload["evidence"][name] = content
            except (OSError, ValueError):
                gaps.append(f"evidence_unavailable:{name}")
        try:
            records = usage_report(journal.repository, ticket)["records"]
            payload["workflow_calls"] = [
                {
                    key: record.get(key)
                    for key in (
                        "id",
                        "stage",
                        "role",
                        "provider",
                        "provider_type",
                        "configured_model",
                        "prompt_sha256",
                        "status",
                        "usage",
                    )
                }
                for record in records
            ]
        except (OSError, ValueError, KeyError, TypeError):
            payload["workflow_calls"] = []
            gaps.append("model_call_provenance_unavailable")
        target = Path(run["worktree_path"]) if run["worktree_path"] else None
        initial = None
        if plan and target:
            try:
                record = json.loads(read_artifact(plan / "worktree.json"))
                base = record["base_commit"]
                if not re.fullmatch(r"[0-9a-f]{40,64}", base):
                    raise ValueError("Invalid base")
                common = git(target, "rev-parse", "--path-format=absolute", "--git-common-dir")
                expected = git(
                    journal.repository, "rev-parse", "--path-format=absolute", "--git-common-dir"
                )
                if (
                    record["path"] != str(target)
                    or common != expected
                    or Path(git(target, "rev-parse", "--show-toplevel").decode().strip()).resolve()
                    != target.resolve()
                ):
                    raise ValueError("Wrong worktree")
                initial = await capture_snapshot(target, base)
                payload["source"] = {
                    "base_commit": base,
                    "snapshot_sha256": initial.fingerprint,
                    "content_sha256": initial.content_fingerprint,
                }
                old = tree(target, base)
                raw = git(target, "diff", "--name-only", "--no-renames", "-z", base, "--")
                raw += git(target, "ls-files", "--others", "--exclude-standard", "-z")
                names = sorted(set(n.decode() for n in raw.split(b"\0") if n))
                if not names:
                    gaps.append("no_changed_files")
                if len(names) > 200:
                    raise ValueError("Too many changed files")
                for name in names:
                    try:
                        if not safe_name(name):
                            raise ValueError("excluded_path")
                        previous = None
                        if name in old:
                            mode, blob = old[name]
                            if mode not in {"100644", "100755"}:
                                raise ValueError("unsupported_file_type")
                            if int(git(target, "cat-file", "-s", blob)) > MAX_FILE:
                                raise ValueError("oversized_blob")
                            previous = {
                                "mode": mode,
                                "content": text(git(target, "cat-file", "blob", blob)),
                            }
                        p = target / name
                        if not safe(p):
                            raise ValueError("symlink")
                        following = None
                        if p.exists():
                            if not p.is_file() or p.stat().st_size > MAX_FILE:
                                raise ValueError("unsupported_file_type")
                            following = {
                                "mode": "100755" if p.stat().st_mode & 0o111 else "100644",
                                "content": text(p.read_bytes()),
                            }
                        if previous is not None:
                            payload["before"][name] = previous
                        if following is not None:
                            payload["after"][name] = following
                    except (OSError, ValueError):
                        gaps.append("changed_file_withheld:" + name)
                if any(g.startswith("changed_file_withheld:") for g in gaps):
                    payload["before"], payload["after"] = {}, {}
                else:
                    payload["patch"] = await asyncio.to_thread(
                        verify_patch, payload["before"], payload["after"]
                    )
                summary = await summarize(journal, ticket)
                for label in ("validation", "review"):
                    info = summary[label]
                    required = {"approval.json", "validation.json"}
                    if label == "review":
                        required.update({"review.json", "review-status.json"})
                    if (
                        info
                        and info["current"]
                        and "plan" in payload["input"]
                        and required.issubset(payload["evidence"])
                    ):
                        payload["quality"][label] = info["status"]
                if summary["review"] and summary["review"]["findings"]:
                    gaps.append("recorded_review_findings_require_assessment")
                if run["state"] == "MERGED":
                    receipt_text = read_artifact(plan / "merge.json")
                    receipt = json.loads(receipt_text)
                    commit = receipt.get("commit", "")
                    validation = json.loads(payload["evidence"].get("validation.json", "{}"))
                    review = json.loads(payload["evidence"].get("review-status.json", "{}"))
                    if (
                        receipt.get("status") == "MERGED"
                        and receipt.get("base_commit") == base
                        and git(target, "rev-parse", "HEAD").decode().strip() == commit
                        and not git(target, "status", "--porcelain")
                        and receipt.get("snapshot_sha256")
                        == validation.get("snapshot_sha256")
                        == review.get("snapshot_sha256")
                        and receipt.get("snapshot_sha256")
                    ):
                        payload["evidence"]["merge.json"] = receipt_text
                        payload["quality"]["human_acceptance"] = "recorded_merge"
                if (await capture_snapshot(target, base)).fingerprint != initial.fingerprint:
                    raise ValueError("Source changed during collection")
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                gaps.append("source_or_evidence_not_verifiable")
                payload["before"], payload["after"], payload["patch"] = {}, {}, None
                payload["quality"].update(
                    validation="unknown", review="unknown", human_acceptance="unknown"
                )
        else:
            gaps.append("missing_plan_or_worktree")
        if payload["quality"]["human_acceptance"] == "unknown":
            gaps.append("human_acceptance_not_verified")
        if payload["quality"]["validation"] != "PASSED":
            gaps.append("passing_validation_not_bound_to_snapshot")
        if payload["quality"]["review"] != "REVIEWED":
            gaps.append("review_not_bound_to_snapshot")
        if "plan" not in payload["input"]:
            gaps.append("approved_plan_unavailable")
        # Archived evidence must be the exact version inspected, not an earlier/latest mix.
        checked_evidence = dict(payload["evidence"])
        if "plan" in payload["input"]:
            checked_evidence["plan.md"] = payload["input"]["plan"]
        for name, value in checked_evidence.items():
            source = (plan / name) if name == "merge.json" and plan else evidence_sources.get(name)
            try:
                if source is None or read_artifact(source) != value:
                    raise ValueError("Evidence changed")
            except (OSError, ValueError):
                gaps.append("evidence_changed_during_collection:" + name)
                payload["quality"].update(
                    validation="unknown", review="unknown", human_acceptance="unknown"
                )
        gaps.extend(
            [
                "unchanged_repository_context_not_captured",
                "original_provider_input_and_tool_trace_unavailable",
                "training_rights_and_sample_quality_not_assessed",
            ]
        )
        raw_payload = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        if len(raw_payload) > MAX_BUNDLE:
            raise ValueError("Corpus candidate exceeds 20 MB; no sample written")
        sample_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex
        parent = journal.path.parent / "corpus" / "samples" / ticket
        if not safe(parent):
            raise ValueError("Unsafe corpus directory")
        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / ("." + sample_id)
        staging.mkdir()
        try:
            (staging / "bundle.json").write_bytes(raw_payload)
            manifest = {
                "schema_version": 1,
                "sample_id": sample_id,
                "ticket_id": ticket,
                "bundle_sha256": digest(raw_payload),
                "created_at": datetime.now(UTC).isoformat(),
            }
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            staging.rename(parent / sample_id)
        except BaseException:
            shutil.rmtree(staging)
            raise
        return parent / sample_id


def inspect(journal: Journal, ticket: str) -> dict[str, Any]:
    journal.get(ticket)
    parent = journal.path.parent / "corpus" / "samples" / ticket
    if not safe(parent):
        raise ValueError("Unsafe corpus directory")
    entries = (
        sorted(p for p in parent.iterdir() if not p.name.startswith(".")) if parent.exists() else []
    )
    if not entries:
        raise ValueError("No corpus samples; run dev-agent corpus collect TICKET-ID first")
    sample = entries[-1]
    if not safe(sample) or not safe(sample / "bundle.json") or not safe(sample / "manifest.json"):
        raise ValueError("Unsafe corpus sample")
    if (sample / "bundle.json").stat().st_size > MAX_BUNDLE:
        raise ValueError("Oversized corpus sample")
    if (sample / "manifest.json").stat().st_size > 65536:
        raise ValueError("Oversized corpus manifest")
    manifest = json.loads((sample / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("sample_id") != sample.name:
        raise ValueError("Invalid corpus manifest")
    raw = (sample / "bundle.json").read_bytes()
    if digest(raw) != manifest["bundle_sha256"]:
        raise ValueError("Corpus integrity check failed")
    data = json.loads(raw)
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported corpus schema")
    if data["ticket_id"] != ticket or manifest["ticket_id"] != ticket:
        raise ValueError("Corpus ticket mismatch")
    replay = False
    if data["patch"] is not None:
        verify_patch(data["before"], data["after"], data["patch"])
        replay = True
    return {
        "ticket": ticket,
        "sample": str(sample),
        "samples": len(entries),
        "provenance": data["provenance"],
        "integrity": "verified",
        "patch_replay": "verified" if replay else "unavailable",
        "changed_files": sorted(set(data["before"]) | set(data["after"])),
        "quality": data["quality"],
        "gaps": data["gaps"],
        "scope": "Changed text files only; evidence is historical at collection time.",
    }
