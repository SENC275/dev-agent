"""Opt-in, reviewable knowledge documents; never silently promote project rules."""

import re
from pathlib import Path

from dev_agent.git.worktree import git
from dev_agent.models.config import ProjectConfig
from dev_agent.models.finding import Finding
from dev_agent.workflow.planning import _json, _local_file

POLICY = (
    "Implementer/Fixer: update the knowledge document before validation. "
    "Reviewer stays read-only. "
    "This document is explicitly authorized workflow scope in addition to the approved code plan. "
    "Extract at most five reusable facts, conventions, decisions or pitfalls from investigation, "
    "then verify them against the final code. Use the ticket's language for prose. "
    "Each candidate needs one exact source file:line (or file:start-end), a narrow applicability "
    "scope, and a recheck condition. Record decisions only when their reason is supported "
    "by the ticket, human feedback or project documentation. Do not generalize a one-ticket "
    "requirement into a permanent rule. Do not copy secrets, personal paths, environment addresses "
    "or agent execution restrictions into knowledge. Do not duplicate existing knowledge. "
    "Do not edit AGENTS.md, CLAUDE.md or unrelated knowledge documents for this feature. "
    "Keep candidates provisional: merging is the human's acceptance of this snapshot, not proof "
    "that it remains true forever. Keep the file synchronized after fixes and revisions. "
    "Human feedback can reject candidates: remove those entries and use the no-candidates format "
    "with a reason if none remain. Do not recreate rejected entries. "
    "Use exactly the supplied metadata and section/field labels. Replace all example prose and "
    "source locations. Use either 1–5 '## Candidate: <title>' sections or one '## No candidates' "
    "section with a nonempty reason; no other level-two sections. For each candidate use the five "
    "single-line fields shown in the template. Kind must be fact, convention, decision or pitfall. "
    "Reviewer: independently verify every candidate, its evidence, scope and continued accuracy "
    "against the actual code and human feedback. Report unsupported claims, stale references, "
    "duplicates, secrets, unjustified rules or a misleading no-candidates reason as findings. "
    "Do not accept the author's statements as evidence. Do not invent candidates to fill a quota."
)


def knowledge_context(
    directory: Path, config: ProjectConfig, *, investigation: bool = False
) -> dict[str, object]:
    if not config.knowledge.enabled:
        return {"enabled": False}
    run = directory.parent.parent
    ticket = _json(_local_file(run, "investigation.json")).get("ticket_id")
    base = _json(_local_file(directory, "worktree.json")).get("base_commit")
    if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", ticket):
        raise ValueError("Invalid knowledge ticket identity.")
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40,64}", base):
        raise ValueError("Invalid knowledge base commit.")
    header = f"# Knowledge candidates\n\nTicket: {ticket}\nBase commit: {base}\n"
    result: dict[str, object] = {
        "enabled": True,
        "path": f"docs/knowledge/{ticket}.md",
        "policy": POLICY,
        "header": header,
        "template": header
        + (
            "\n## Candidate: Short title\n\n"
            "- Kind: fact\n- Claim: Verified reusable observation\n"
            "- Scope: Where this applies\n- Evidence: path/to/source.py:12\n"
            "- Recheck: When this should be checked again\n"
        ),
        "empty_template": header
        + "\n## No candidates\n\nExplain why nothing should be retained.\n",
    }
    if investigation:
        result["investigation_evidence"] = {
            name: _local_file(run, name).read_text(encoding="utf-8")[:16_000]
            for name in ("exploration.json", "patterns.json", "tests.json")
        }
        result["evidence_note"] = (
            "Investigation excerpts may be truncated at 16000 characters each. "
            "They are leads, not proof; verify against current code."
        )
    return result


async def knowledge_findings(target: Path, context: dict[str, object]) -> list[Finding]:
    if not context.get("enabled"):
        return []
    relative = str(context["path"])
    path = target / relative
    problem = ""
    try:
        # Refuse symlink parents as well as a symlink file, including links within the repo.
        if any(
            p.is_symlink()
            for p in (path, *path.parents)
            if p != target and p.is_relative_to(target)
        ):
            raise ValueError("Knowledge path must not contain symlinks.")
        if not path.is_file() or path.stat().st_size > 32_000:
            raise ValueError("Knowledge document is missing or exceeds 32 KB.")
        visible = await git(
            target, "ls-files", "--cached", "--others", "--exclude-standard", "--", relative
        )
        if relative not in visible.splitlines():
            raise ValueError("Knowledge document is ignored and would not be reviewed or merged.")
        text = path.read_text(encoding="utf-8")
        header = str(context["header"])
        if not text.startswith(header):
            raise ValueError("Knowledge document must retain the exact ticket and base metadata.")
        sections = list(re.finditer(r"^## (.+)$", text, re.MULTILINE))
        if not sections or text[len(header) : sections[0].start()].strip():
            raise ValueError("Use the supplied candidate or no-candidates format.")
        if len(sections) == 1 and sections[0].group(1) == "No candidates":
            if not text[sections[0].end() :].strip():
                raise ValueError("No candidates requires a reason.")
        else:
            if len(sections) > 5:
                raise ValueError("Keep at most five knowledge candidates.")
            for index, section in enumerate(sections):
                if (
                    not section.group(1).startswith("Candidate: ")
                    or not section.group(1)[11:].strip()
                ):
                    raise ValueError("Each candidate needs a title.")
                end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
                body = text[section.end() : end].strip()
                fields = re.fullmatch(
                    r"- Kind: (fact|convention|decision|pitfall)\n"
                    r"- Claim: ([^\n]+)\n- Scope: ([^\n]+)\n"
                    r"- Evidence: ([^\n]+)\n- Recheck: ([^\n]+)",
                    body,
                )
                if not fields or not all(value.strip() for value in fields.groups()):
                    raise ValueError("Candidates require Kind, Claim, Scope, Evidence and Recheck.")
                evidence = re.fullmatch(r"(.+):([1-9][0-9]*)(?:-([1-9][0-9]*))?", fields.group(4))
                if not evidence:
                    raise ValueError(
                        "Evidence must be one repository-relative file:line or file:start-end."
                    )
                source = Path(evidence.group(1))
                if (
                    source.is_absolute()
                    or any(
                        p in {"..", ".git", ".dev-agent"} or p.startswith(".env")
                        for p in source.parts
                    )
                    or source.as_posix().startswith("docs/knowledge/")
                ):
                    raise ValueError(
                        "Evidence must reference project code, tests or decision documentation."
                    )
                proof = target / source
                if (
                    not proof.is_file()
                    or not proof.resolve().is_relative_to(target)
                    or any(
                        p.is_symlink()
                        for p in (proof, *proof.parents)
                        if p != target and p.is_relative_to(target)
                    )
                    or proof.stat().st_size > 2 * 1024 * 1024
                ):
                    raise ValueError("Evidence must reference an existing regular project file.")
                visible = await git(
                    target,
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--",
                    str(source),
                )
                if str(source) not in visible.splitlines():
                    raise ValueError("Evidence must not reference an ignored file.")
                start = int(evidence.group(2))
                end_line = int(evidence.group(3) or evidence.group(2))
                if end_line < start or end_line > len(
                    proof.read_text(encoding="utf-8").splitlines()
                ):
                    raise ValueError("Evidence line does not exist or its range is reversed.")
    except (ValueError, OSError) as exc:
        problem = (
            str(exc)
            if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError)
            else "Knowledge or evidence could not be read as UTF-8."
        )
    if not problem:
        return []
    return [
        Finding(
            severity="medium",
            category="knowledge",
            file=relative,
            line=1,
            scenario="This ticket is merged with missing or unverifiable project knowledge.",
            impact=problem,
            recommendation=(
                "Repair the document using the supplied contract, or record No candidates "
                "with a reason. Respect human rejections."
            ),
        )
    ]
