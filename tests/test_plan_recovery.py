import asyncio
import json
from pathlib import Path

import pytest
from test_persistence import PipelineProvider
from test_validation import commands
from test_worktree import repository as repository

from dev_agent.models.agent import AgentResult
from dev_agent.models.config import GatesConfig, ProjectConfig
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.agent_plan_gate import review_plan_by_agent
from dev_agent.workflow.persistence import Journal
from dev_agent.workflow.planning import HEADINGS, approval_is_current
from dev_agent.workflow.runner import advance

APPROVE = {"decision": "approve", "summary": "Checked.", "issues": []}
REJECT = {"decision": "request_changes", "summary": "Needs tests.", "issues": ["Add edge tests."]}
CONFLICT = {"decision": "approve", "summary": "Checked.", "issues": ["Missing tests."]}


class SequenceProvider(PipelineProvider):
    def __init__(self, responses, *, revision="valid", mutate=False):
        super().__init__()
        self.responses = responses
        self.revision = revision
        self.mutate = mutate
        self.reviews = 0
        self.revisions = 0

    async def execute(self, task):
        if task.role == "reviewer" and "proposed implementation plan" in task.prompt:
            self.tasks.append(task)
            response = self.responses[min(self.reviews, len(self.responses) - 1)]
            self.reviews += 1
            if self.mutate:
                (task.working_directory / "app.txt").write_text("unexpected edit")
            return AgentResult(
                success=response != "provider_failure",
                exit_code=1 if response == "provider_failure" else 0,
                duration_seconds=0,
                output=json.dumps(response) if isinstance(response, dict) else response,
            )
        if task.role == "planner" and "Revise the existing plan" in task.prompt:
            self.tasks.append(task)
            self.revisions += 1
            assert task.read_only and "Add edge tests." in task.prompt
            text = "\n".join(f"## {heading}\nRevised with edge tests.\n" for heading in HEADINGS)
            if self.revision == "invalid":
                text = "Not a complete plan"
            return AgentResult(success=True, exit_code=0, duration_seconds=0, output=text)
        return await super().execute(task)


def setup(repo, responses, **kwargs):
    provider = SequenceProvider(responses, **kwargs)
    config = ProjectConfig(commands=commands(), gates=GatesConfig(plan_review="agent"))
    journal = Journal(repo)
    journal.create("RECOVER", "Change app", config.model_dump_json())
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    return journal, config, provider, registry


@pytest.mark.parametrize("bad", ["not JSON", CONFLICT])
def test_one_correction_can_recover_without_replanning(repository, bad):
    journal, config, provider, registry = setup(repository, [bad, APPROVE])
    assert asyncio.run(advance(journal, "RECOVER", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert provider.reviews == 2 and provider.revisions == 0
    correction = [t for t in provider.tasks if t.role == "reviewer"][1]
    assert "validation_error" in correction.prompt and "previous_response" in correction.prompt
    plan = Path(journal.get("RECOVER")["plan_path"])
    assert len(list((plan / "plan-review-attempts").iterdir())) == 2
    assert asyncio.run(approval_is_current(plan))


def test_correction_can_reject_then_planner_revises_and_reviewer_approves(repository):
    journal, config, provider, registry = setup(repository, [CONFLICT, REJECT, APPROVE])
    assert asyncio.run(advance(journal, "RECOVER", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert provider.reviews == 3 and provider.revisions == 1
    plan = Path(journal.get("RECOVER")["plan_path"])
    assert "Small change" in (plan / "plan-revisions/0001/before.md").read_text()
    assert "Revised with edge tests" in (plan / "plan.md").read_text()
    receipt = json.loads((plan / "plan-review.json").read_text())
    approval = json.loads((plan / "approval.json").read_text())
    assert receipt["plan_sha256"] == approval["plan_sha256"]
    assert asyncio.run(approval_is_current(plan))
    assert [t.role for t in provider.tasks].count("implementer") == 1


def test_persistent_rejection_exhausts_budget_without_worktree_or_reset(repository):
    journal, config, provider, registry = setup(repository, [REJECT])
    assert asyncio.run(advance(journal, "RECOVER", config, registry)) == "AWAITING_PLAN_APPROVAL"
    assert provider.revisions == 2 and provider.reviews == 3
    plan = Path(journal.get("RECOVER")["plan_path"])
    assert not (plan / "worktree.json").exists() and not (plan / "approval.json").exists()
    assert "[plan_rejected]" in journal.get("RECOVER")["error"]
    assert not asyncio.run(review_plan_by_agent(plan, registry))
    assert provider.revisions == 2  # Restart does not replenish persisted budget.
    assert all(t.read_only for t in provider.tasks)


def test_execution_failure_is_not_retried_as_output_error(repository):
    journal, config, provider, registry = setup(repository, ["provider_failure"])
    with pytest.raises(ValueError, match="provider_execution"):
        asyncio.run(advance(journal, "RECOVER", config, registry))
    assert provider.reviews == 1 and provider.revisions == 0


def test_mutating_bad_response_is_not_corrected(repository):
    journal, config, provider, registry = setup(repository, ["not JSON"], mutate=True)
    with pytest.raises(ValueError, match="inputs_changed"):
        asyncio.run(advance(journal, "RECOVER", config, registry))
    assert provider.reviews == 1


def test_invalid_revised_plan_preserves_original_and_spends_budget(repository):
    journal, config, provider, registry = setup(repository, [REJECT], revision="invalid")
    with pytest.raises(ValueError, match="model_output"):
        asyncio.run(advance(journal, "RECOVER", config, registry))
    plan = Path(journal.get("RECOVER")["plan_path"])
    assert "Small change" in (plan / "plan.md").read_text()
    assert not (plan / "approval.json").exists()
    provider.revision = "valid"
    assert asyncio.run(advance(journal, "RECOVER", config, registry)) == "AWAITING_PLAN_APPROVAL"
    assert provider.revisions == 2  # Failed planner output consumed the first attempt.
