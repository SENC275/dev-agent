import json

import pytest

from dev_agent.artifacts import ArtifactValidationError, parse_artifact
from dev_agent.models.finding import Finding, Review
from dev_agent.models.investigation import Exploration, PatternAnalysis
from dev_agent.models.investigation import TestAnalysis as Analysis

EXPLORATION = {
    "entry_points": ["src/api.py:42"],
    "call_chain": ["Router.create", "Service.create"],
    "database_writes": [],
    "external_calls": ["Client.create"],
    "important_files": ["src/api.py"],
    "risks": ["Upstream timeout after successful creation"],
}
FINDING = {
    "severity": "high",
    "category": "idempotency",
    "file": "src/service.py",
    "line": 182,
    "scenario": "Upstream succeeds before timeout",
    "impact": "Duplicate creation",
    "recommendation": "Check idempotency support",
}


def test_readme_examples_and_round_trip() -> None:
    exploration = parse_artifact(json.dumps(EXPLORATION), Exploration)
    assert exploration.entry_points == ["src/api.py:42"]
    assert exploration.assumptions == []
    pattern = parse_artifact(
        json.dumps(
            {
                "patterns": [
                    {
                        "name": "Retry",
                        "files": ["src/client.py:81"],
                        "description": "Reuse retry policy",
                    }
                ]
            }
        ),
        PatternAnalysis,
    )
    assert pattern.patterns[0].files == ["src/client.py:81"]
    tests = parse_artifact(
        json.dumps(
            {
                "existing_tests": ["tests/test_service.py"],
                "fixtures": ["client"],
                "recommended_tests": ["timeout"],
                "assumptions": ["Retries are allowed"],
                "unresolved_questions": ["Does upstream support idempotency?"],
            }
        ),
        Analysis,
    )
    assert tests.assumptions == ["Retries are allowed"]
    review = parse_artifact(json.dumps({"findings": [FINDING]}), Review)
    assert review.findings[0].line == 182
    assert parse_artifact(review.model_dump_json(), Review) == review
    assert parse_artifact(tests.model_dump_json(), Analysis) == tests
    assert parse_artifact(exploration.model_dump_json(), Exploration) == exploration
    assert parse_artifact(pattern.model_dump_json(), PatternAnalysis) == pattern
    assert parse_artifact(json.dumps(FINDING), Finding) == review.findings[0]


def test_explicit_empty_results_are_valid() -> None:
    assert parse_artifact('{"findings": []}', Review).findings == []
    assert parse_artifact('{"patterns": []}', PatternAnalysis).patterns == []
    assert parse_artifact(json.dumps({key: [] for key in EXPLORATION}), Exploration).risks == []


@pytest.mark.parametrize(
    "output",
    [
        "",
        "not json",
        '{"findings":',
        '```json\n{"findings": []}\n```',
        '{"findings": []} trailing',
        '{"findings": []}{"findings": []}',
        '{"findings": [], "findings": []}',
        '{"findings": NaN}',
        '{"findings": Infinity}',
    ],
)
def test_invalid_json(output: str) -> None:
    with pytest.raises(ArtifactValidationError, match="Review:"):
        parse_artifact(output, Review)


@pytest.mark.parametrize(
    "output", ["{}", "[]", "null", "1", '{"findings": null}', '{"findings": [], "unexpected": 1}']
)
def test_invalid_review_shape(output: str) -> None:
    with pytest.raises(ArtifactValidationError):
        parse_artifact(output, Review)


@pytest.mark.parametrize(
    "field,value",
    [
        ("severity", "urgent"),
        ("line", "182"),
        ("line", True),
        ("line", 1.5),
        ("line", 0),
        ("line", -1),
        ("scenario", "  "),
        ("file", 123),
        ("impact", None),
        ("extra", "unknown"),
    ],
)
def test_nested_validation_location(field: str, value: object) -> None:
    finding = dict(FINDING, **{field: value})
    with pytest.raises(ArtifactValidationError, match=rf"findings\.0\.{field}"):
        parse_artifact(json.dumps({"findings": [finding]}), Review)


def test_missing_field_not_silently_defaulted() -> None:
    with pytest.raises(ArtifactValidationError, match="findings: missing"):
        parse_artifact("{}", Review)
    data = dict(EXPLORATION)
    del data["risks"]
    with pytest.raises(ArtifactValidationError, match="risks: missing"):
        parse_artifact(json.dumps(data), Exploration)


@pytest.mark.parametrize("files", [[], [" "], "src/client.py:81"])
def test_pattern_requires_evidence(files: object) -> None:
    with pytest.raises(ArtifactValidationError, match=r"patterns\.0\.files"):
        parse_artifact(
            json.dumps(
                {
                    "patterns": [
                        {
                            "name": "Retry",
                            "files": files,
                            "description": "Existing policy",
                        }
                    ]
                }
            ),
            PatternAnalysis,
        )


def test_nested_duplicate_key() -> None:
    with pytest.raises(ArtifactValidationError, match="duplicate"):
        parse_artifact('{"findings": [{"line": 1, "line": 2}]}', Review)


def test_errors_do_not_echo_response_values() -> None:
    finding = dict(FINDING, severity="secret-token-do-not-print")
    with pytest.raises(ArtifactValidationError) as error:
        parse_artifact(json.dumps({"findings": [finding]}), Review)
    assert "secret-token" not in str(error.value)
    assert "literal_error" in str(error.value)


def test_json_schema_is_available() -> None:
    schema = Review.model_json_schema()
    assert schema["required"] == ["findings"]
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Finding"]["properties"]["severity"]["enum"] == [
        "critical",
        "high",
        "medium",
        "low",
    ]


@pytest.mark.parametrize("severity", ["critical", "high", "medium", "low"])
def test_allowed_severities(severity: str) -> None:
    finding = parse_artifact(json.dumps(dict(FINDING, severity=severity)), Finding)
    assert finding.severity == severity


def test_test_analysis_required_fields() -> None:
    with pytest.raises(ArtifactValidationError, match="recommended_tests: missing"):
        parse_artifact('{"existing_tests": [], "fixtures": []}', Analysis)
    result = parse_artifact(
        '{"existing_tests": [], "fixtures": [], "recommended_tests": []}',
        Analysis,
    )
    assert result.recommended_tests == []


def test_deeply_nested_json_fails_clearly() -> None:
    with pytest.raises(ArtifactValidationError, match="Review:"):
        parse_artifact("[" * 2000 + "]" * 2000, Review)
