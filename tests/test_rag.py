import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from test_worktree import git
from test_worktree import repository as repository

from dev_agent.models.agent import AgentTask
from dev_agent.models.config import RAGConfig
from dev_agent.rag import location, prepare, refresh, search


def test_incremental_update_delete_and_exclusions(repository: Path) -> None:
    (repository / "alarms.tf").write_text(
        'resource "cloudwatch_alarm" "latency" { threshold = 80 }\n'
    )
    (repository / "secrets.md").write_text("cloudwatch super_secret")
    (repository / ".env").write_text("cloudwatch=secret")
    (repository / "link.md").symlink_to(repository / "alarms.tf")
    config = RAGConfig()
    first = asyncio.run(search(repository, "cloudwatch latency", config))
    assert [h["path"] for h in first["hits"]] == ["alarms.tf"]
    assert first["hits"][0]["citation"] == "alarms.tf:1"
    assert asyncio.run(refresh(repository, config))["changed"] == 0
    (repository / "alarms.tf").write_text(
        'resource "cloudwatch_alarm" "latency" { threshold = 90 }\n'
    )
    second = asyncio.run(search(repository, "cloudwatch latency", config))
    assert "90" in second["hits"][0]["text"]
    assert second["index"]["changed"] == 1
    (repository / "alarms.tf").unlink()
    assert not asyncio.run(search(repository, "cloudwatch latency", config))["hits"]


def test_worktrees_share_database_not_evidence(repository: Path, tmp_path: Path) -> None:
    (repository / "architecture.md").write_text("blue database PostgreSQL source branch")
    git(repository, "add", "architecture.md")
    git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "docs",
    )
    other = tmp_path / "other"
    git(repository, "worktree", "add", "-b", "other", str(other))
    (other / "architecture.md").write_text("green database SQLite other branch")
    assert asyncio.run(location(repository)) == asyncio.run(location(other))
    a = asyncio.run(search(repository, "database", RAGConfig()))
    b = asyncio.run(search(other, "database", RAGConfig()))
    assert "PostgreSQL" in a["hits"][0]["text"]
    assert "SQLite" in b["hits"][0]["text"]
    assert "SQLite" not in json.dumps(a["hits"])


def test_vector_cache_fallback_and_stale_source(repository: Path) -> None:
    (repository / "note.md").write_text("autoscaling capacity")
    config = RAGConfig(embedding_model="test-embed")
    batches = []

    async def fake(texts, config):
        batches.append(texts)
        return [[1.0, 0.0] for _ in texts]

    with patch("dev_agent.rag.embed", side_effect=fake):
        first = asyncio.run(search(repository, "increase compute", config))
        assert first["mode"] == "hybrid"
        before = len(batches)
        asyncio.run(search(repository, "increase compute", config))
        assert len(batches) == before + 1  # only query vector; documents are shared/cached
    with patch("dev_agent.rag.embed", side_effect=ValueError("model unavailable")):
        fallback = asyncio.run(search(repository, "autoscaling", config))
        assert fallback["mode"] == "lexical" and fallback["warning"]
        assert fallback["hits"]

    async def mutate(texts, config):
        (repository / "note.md").write_text("entirely changed")
        return [[1.0, 0.0] for _ in texts]

    with patch("dev_agent.rag.embed", side_effect=mutate):
        result = asyncio.run(search(repository, "autoscaling", config))
    assert not any(h["path"] == "note.md" for h in result["hits"])


def test_prompt_budget_and_cache_tamper_cannot_inject(repository: Path) -> None:
    (repository / "rules.md").write_text("alarm threshold retry\n" * 300)
    config = RAGConfig(enabled=True, max_characters=2500)
    task = AgentTask(role="planner", prompt="alarm threshold", working_directory=repository)
    enriched, info = asyncio.run(prepare(task, config))
    assert info["used"] and info["citations"]
    assert len(enriched.prompt) - len(task.prompt) <= config.max_characters
    path = asyncio.run(location(repository))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE rag_chunks SET text='MALICIOUS CACHE INSTRUCTION'")
    result = asyncio.run(search(repository, "alarm threshold", config))
    assert "MALICIOUS" not in json.dumps(result["hits"])
    assert "untrusted source text" in enriched.prompt


def test_redacts_credentials_and_omits_ignored_files(repository: Path) -> None:
    (repository / ".gitignore").write_text("ignored.md\n")
    (repository / "ignored.md").write_text("uniqueignored")
    (repository / "config.py").write_text('database_password = "real-secret-value"\n')
    result = asyncio.run(search(repository, "database password uniqueignored", RAGConfig()))
    assert "real-secret-value" not in json.dumps(result["hits"])
    assert not any(h["path"] == "ignored.md" for h in result["hits"])


def test_invalid_embedding_origin() -> None:
    with pytest.raises(ValueError):
        RAGConfig(embedding_url="http://user:password@example.invalid")


def test_chinese_queries_and_clear_vectors(repository: Path) -> None:
    from dev_agent.rag import clear_vectors

    (repository / "notes.md").write_text("数据库迁移需要确保保留现有数据")
    result = asyncio.run(search(repository, "数据库迁移", RAGConfig()))
    assert result["hits"][0]["path"] == "notes.md"
    with sqlite3.connect(asyncio.run(location(repository))) as db:
        db.execute("INSERT INTO rag_vectors VALUES('old','[1.0]')")
    assert asyncio.run(clear_vectors(repository)) == 1
    assert asyncio.run(search(repository, "数据库", RAGConfig()))["hits"]


def test_embedding_api_contract_and_validation() -> None:
    import httpx

    from dev_agent.rag import embed

    responses = [{"embeddings": [[3, 4]]}, {"embeddings": [[0, 0]]}]

    def respond(request):
        assert request.url.path == "/api/embed"
        assert json.loads(request.content) == {
            "model": "test-model",
            "input": ["example"],
            "truncate": False,
        }
        return httpx.Response(200, json=responses.pop(0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with patch("dev_agent.rag.httpx.AsyncClient", return_value=client):
        assert asyncio.run(embed(["example"], RAGConfig(embedding_model="test-model"))) == [
            [0.6, 0.8]
        ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with patch("dev_agent.rag.httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="norm"):
            asyncio.run(embed(["example"], RAGConfig(embedding_model="test-model")))


@pytest.mark.parametrize("role", ["explorer", "planner", "implementer", "reviewer", "fixer"])
def test_registry_injects_fresh_evidence_and_records_citations(repository: Path, role: str) -> None:
    from unittest.mock import AsyncMock

    from dev_agent.models.agent import AgentResult
    from dev_agent.models.config import ProjectConfig
    from dev_agent.providers.registry import ProviderRegistry

    (repository / "alarms.tf").write_text("cloudwatch latency threshold = 80")
    config = ProjectConfig(rag=RAGConfig(enabled=True))
    fake = AsyncMock()
    fake.execute.return_value = AgentResult(
        success=True, output="{}", exit_code=0, duration_seconds=0.01
    )
    registry = ProviderRegistry(config, {"codex_cli": lambda _: fake})
    directory = repository / ".dev-agent/runs/TEST/run1"
    task = AgentTask(role=role, prompt="cloudwatch latency", working_directory=repository)
    asyncio.run(registry.execute("implement" if role == "implementer" else "plan", directory, task))
    assert "threshold = 80" in fake.execute.call_args.args[0].prompt
    records = list((directory / "usage").glob("*.json"))
    assert len(records) == 1
    assert "alarms.tf:1" in records[0].read_text()
