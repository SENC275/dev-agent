"""Human feedback supplied separately from model-generated summaries."""

from pathlib import Path

from dev_agent.workflow.planning import _hash, _json, _local_file


def feedback_history(directory: Path) -> list[str]:
    root = directory / "revisions"
    if root.is_symlink():
        raise ValueError("Refusing symlink revision history.")
    if not root.exists():
        return []
    result = []
    for item in sorted(root.iterdir()):
        if item.is_symlink() or not item.is_dir():
            raise ValueError("Invalid revision history.")
        data = _local_file(item, "feedback.md").read_bytes()
        record = _json(_local_file(item, "revision.json"))
        if record.get("feedback_sha256") != _hash(data):
            raise ValueError("Human feedback changed; inspect revision artifacts.")
        result.append(data.decode("utf-8"))
    return result
