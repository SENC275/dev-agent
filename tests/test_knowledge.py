import asyncio
import json
from pathlib import Path

import pytest
from test_persistence import PipelineProvider
from test_validation import commands
from test_worktree import git
from test_worktree import repository as repository

from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import KnowledgeConfig, ProjectConfig
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.validation.runner import validate
from dev_agent.workflow.knowledge import knowledge_context, knowledge_findings
from dev_agent.workflow.merging import merge, preview_merge
from dev_agent.workflow.persistence import Journal
from dev_agent.workflow.planning import approve_plan
from dev_agent.workflow.review import review
from dev_agent.workflow.revision import revise
from dev_agent.workflow.runner import advance


def content(context: dict[str, object]) -> str:
    return str(context["template"]).replace("path/to/source.py:12", "app.txt:1")


class KnowledgeProvider(PipelineProvider):
    async def execute(self, task: AgentTask) -> AgentResult:
        if task.role == "fixer":
            self.tasks.append(task)
            bundle = json.loads(task.prompt.split("Input bundle:\n", 1)[1])
            context = bundle["knowledge"]
            (task.working_directory / context["path"]).write_text(content(context))
            output = json.dumps(
                {
                    "summary": "Corrected the document.",
                    "decisions": [
                        {
                            "finding_index": i,
                            "disposition": "fixed",
                            "explanation": "Restored the source reference.",
                            "evidence": ["app.txt:1"],
                        }
                        for i, _ in enumerate(bundle["findings"]["findings"])
                    ],
                }
            )
            return AgentResult(success=True, output=output, exit_code=0, duration_seconds=0)
        result = await super().execute(task)
        if task.role == "implementer":
            bundle = json.loads(task.prompt.split("Input bundle:\n", 1)[1])
            context = bundle["knowledge"]
            path = task.working_directory / context["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            text = content(context)
            if bundle.get("human_feedback"):
                text = (
                    str(context["header"]) + "\n## No candidates\n\nRejected by human feedback.\n"
                )
            path.write_text(text)
        return result


@pytest.fixture
def knowledge_ready(repository: Path) -> tuple:
    config = ProjectConfig(commands=commands(), knowledge=KnowledgeConfig(enabled=True))
    journal = Journal(repository)
    provider = KnowledgeProvider()
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    journal.create("DEMO-12", "Change app", config.model_dump_json())
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "AWAITING_PLAN_APPROVAL"
    directory = Path(journal.get("DEMO-12")["plan_path"])
    asyncio.run(approve_plan(directory, (directory / "plan.md").read_text()))
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    target = Path(journal.get("DEMO-12")["worktree_path"])
    return journal, config, provider, registry, directory, target


def test_candidates_reviewed_merged_and_provenance_preserved(knowledge_ready: tuple) -> None:
    journal, config, provider, _, directory, target = knowledge_ready
    context = knowledge_context(directory, config)
    path = str(context["path"])
    assert not (journal.repository / path).exists()
    implementer = next(t for t in provider.tasks if t.role == "implementer")
    assert "investigation_evidence" in implementer.prompt
    reviewer = next(t for t in provider.tasks if t.role == "reviewer")
    assert path in reviewer.prompt and "Recheck:" in reviewer.prompt
    assert (
        "investigation_evidence" not in reviewer.prompt and "agent_summary" not in reviewer.prompt
    )
    assert len(provider.tasks) == 6  # No extra model call or approval gate.
    git(journal.repository, "config", "user.name", "Test")
    git(journal.repository, "config", "user.email", "test@example.invalid")
    preview = asyncio.run(preview_merge(journal, "DEMO-12"))
    asyncio.run(merge(journal, "DEMO-12", preview, "Accept code and knowledge"))
    assert (journal.repository / path).read_text() == (target / path).read_text()
    assert preview.base in (target / path).read_text()


@pytest.mark.parametrize("mode", ["missing", "invalid", "metadata", "line", "ignored", "symlink"])
def test_bad_candidates_cannot_pass_an_empty_model_review(
    knowledge_ready: tuple, mode: str
) -> None:
    journal, config, _, registry, directory, target = knowledge_ready
    context = knowledge_context(directory, config)
    path = target / str(context["path"])
    if mode == "missing":
        path.unlink()
    elif mode == "invalid":
        path.write_text("An unsupported project rule")
    elif mode == "metadata":
        path.write_text(content(context).replace("Ticket: DEMO-12", "Ticket: WRONG"))
    elif mode == "line":
        path.write_text(content(context).replace("app.txt:1", "app.txt:9999"))
    elif mode == "ignored":
        with (journal.repository / ".git/info/exclude").open("a") as stream:
            stream.write("\ndocs/knowledge/\n")
    else:
        path.unlink()
        path.symlink_to(target / "app.txt")
    asyncio.run(validate(directory, config.commands, config.validation))
    asyncio.run(review(directory, registry))
    findings = json.loads((directory / "review.json").read_text())["findings"]
    assert any(f["category"] == "knowledge" for f in findings)
    with pytest.raises(ValueError):
        asyncio.run(preview_merge(journal, "DEMO-12"))


def test_revision_can_reject_candidates_without_recreating_them(knowledge_ready: tuple) -> None:
    journal, config, provider, registry, directory, target = knowledge_ready
    assert (
        asyncio.run(revise(journal, "DEMO-12", "Reject all knowledge candidates", config, registry))
        == "READY_FOR_HUMAN_REVIEW"
    )
    context = knowledge_context(directory, config)
    assert "## No candidates" in (target / str(context["path"])).read_text()
    assert not asyncio.run(knowledge_findings(target, context))
    task = [t for t in provider.tasks if t.role == "implementer"][-1]
    assert "Do not recreate rejected entries" in task.prompt


def test_later_knowledge_edit_invalidates_merge(knowledge_ready: tuple) -> None:
    journal, config, _, _, directory, target = knowledge_ready
    path = target / str(knowledge_context(directory, config)["path"])
    path.write_text(
        path.read_text().replace("Verified reusable observation", "Different assertion")
    )
    with pytest.raises(ValueError):
        asyncio.run(preview_merge(journal, "DEMO-12"))


def test_old_config_does_not_read_or_require_knowledge(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate({})
    assert not config.knowledge.enabled
    assert knowledge_context(tmp_path, config) == {"enabled": False}
    assert not asyncio.run(knowledge_findings(tmp_path, {"enabled": False}))


def test_fixer_repairs_document_and_fresh_review_checks_it(knowledge_ready: tuple) -> None:
    from dev_agent.workflow.fixing import fix

    _, config, provider, registry, directory, target = knowledge_ready
    context = knowledge_context(directory, config)
    path = target / str(context["path"])
    path.write_text(content(context).replace("app.txt:1", "app.txt:900"))
    asyncio.run(validate(directory, config.commands, config.validation))
    asyncio.run(review(directory, registry))
    assert asyncio.run(fix(directory, config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert "app.txt:1" in path.read_text()
    fixer = next(t for t in provider.tasks if t.role == "fixer")
    assert "knowledge" in fixer.prompt and "Evidence line does not exist" in fixer.prompt
    assert provider.tasks[-1].role == "reviewer"


@pytest.mark.parametrize(
    "reference", ["../outside:1", ".env:1", ".git/config:1", "/tmp/secret:1", "missing.py:1"]
)
def test_unsafe_evidence_is_rejected(knowledge_ready: tuple, reference: str) -> None:
    _, config, _, _, directory, target = knowledge_ready
    context = knowledge_context(directory, config)
    path = target / str(context["path"])
    path.write_text(content(context).replace("app.txt:1", reference))
    assert asyncio.run(knowledge_findings(target, context))


@pytest.mark.parametrize(
    "reference, valid", [("app.txt:1-1", True), ("app.txt:2-1", False), ("app.txt:1-900", False)]
)
def test_evidence_ranges(knowledge_ready: tuple, reference: str, valid: bool) -> None:
    _, config, _, _, directory, target = knowledge_ready
    context = knowledge_context(directory, config)
    path = target / str(context["path"])
    path.write_text(content(context).replace("app.txt:1", reference))
    assert bool(asyncio.run(knowledge_findings(target, context))) is not valid
