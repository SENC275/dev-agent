"""Capture reviewable changes and a deterministic fingerprint of worktree inputs."""

import difflib
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from dev_agent.git.worktree import git


@dataclass(frozen=True)
class Snapshot:
    fingerprint: str
    diff: str
    content_fingerprint: str


async def capture_snapshot(target: Path, base: str) -> Snapshot:
    tracked = await git(target, "ls-files", "-z")
    untracked = await git(
        target,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        ".",
        ":(exclude).dev-agent/**",
    )
    additions = set(filter(None, untracked.split("\x00")))
    entries = []
    diff = ""
    for name in sorted(set(filter(None, (tracked + untracked).split("\x00")))):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Snapshot contains an unsafe repository path.")
        if any(part == ".env" or part.startswith(".env.") for part in relative.parts):
            raise ValueError(
                "Snapshot cannot read .env files; remove them from tracked/nonignored inputs."
            )
        path = target / relative
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
            mode = "symlink"
        elif not path.exists():
            entries.append((name, "deleted", ""))
            continue
        else:
            if not path.is_file() or not path.resolve().is_relative_to(target):
                raise ValueError(
                    "Snapshot requires regular files or symlinks; submodules are unsupported."
                )
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError(f"Snapshot file exceeds the 2 MiB limit: {name}")
            data = path.read_bytes()
            mode = "executable" if path.stat().st_mode & 0o111 else "file"
        entries.append((name, mode, hashlib.sha256(data).hexdigest()))
        if name in additions:
            try:
                text = data.decode("utf-8")
                if "\x00" in text:
                    raise UnicodeError()
                diff += "\n" + "".join(
                    difflib.unified_diff(
                        [],
                        text.splitlines(keepends=True),
                        fromfile="/dev/null",
                        tofile=f"b/{name}",
                    )
                )
                if text and not text.endswith("\n"):
                    diff += "\n\\ No newline at end of file\n"
                if not text:
                    diff += f"\nNew empty {mode}: {name}\n"
            except UnicodeError:
                diff += f"\nNew binary {mode}: {name} (sha256 {hashlib.sha256(data).hexdigest()})\n"
    tracked_diff = await git(
        target, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", base, "--"
    )
    diff = (tracked_diff + "\n" if tracked_diff else "") + diff
    staged = await git(
        target, "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", base, "--"
    )
    payload = json.dumps([base, entries, diff, staged], ensure_ascii=True).encode()
    contents = json.dumps([entry for entry in entries if entry[1] != "deleted"]).encode()
    return Snapshot(hashlib.sha256(payload).hexdigest(), diff, hashlib.sha256(contents).hexdigest())
