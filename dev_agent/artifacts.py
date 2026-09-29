"""Strict parsing of a complete JSON artifact, independent of any provider."""

import json

from pydantic import ValidationError

from dev_agent.models.artifact import ArtifactModel


class ArtifactValidationError(ValueError):
    """An agent response does not satisfy the requested artifact contract."""


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            # Do not include arbitrary response contents in diagnostics.
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError("non-finite numeric constant")


def parse_artifact[Artifact: ArtifactModel](output: str, model: type[Artifact]) -> Artifact:
    """Parse one complete JSON object without coercion or speculative repair.

    Reject duplicate keys and non-standard NaN/Infinity tokens at every depth.
    Field errors include location and error type, but never the raw response.
    File references are evidence supplied by the model, not verified facts.
    """
    try:
        data = json.loads(output, object_pairs_hook=_object, parse_constant=_constant)
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(
            f"{model.__name__}: invalid JSON at line {exc.lineno}, column {exc.colno}."
        ) from None
    except (ValueError, RecursionError) as exc:
        reason = "JSON nesting too deep" if isinstance(exc, RecursionError) else str(exc)
        raise ArtifactValidationError(f"{model.__name__}: {reason}.") from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['type']}"
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        raise ArtifactValidationError(f"{model.__name__}: {details}") from None
