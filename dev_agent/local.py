"""Keep operational files out of Git using repository-local exclusions."""

import subprocess
from pathlib import Path


def protect_local_files(repository: Path) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return  # init also supports preparing a directory before git init.
    path = Path(result.stdout.strip())
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Refusing symlink Git exclude file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    rules = [
        rule for rule in ("/.dev-agent/", "/.dev-agent.yaml") if rule not in content.splitlines()
    ]
    if rules:
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n# dev-agent local files (not shared with the repository)\n")
            stream.write("\n".join(rules) + "\n")
