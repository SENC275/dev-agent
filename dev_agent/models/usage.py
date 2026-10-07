"""Provider-reported usage. Missing metrics stay unknown, never estimated from text."""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Total input INCLUDES cache reads/writes. Cache fields are subsets, not additions.
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    source: str = "unavailable"
    complete: bool = False
    reported_requests: int | None = None
    models: list[str] = Field(default_factory=list)


def count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def total(values: list[int | None]) -> int | None:
    return sum(v for v in values if v is not None) if values and None not in values else None


def combine(items: list[TokenUsage], source: str) -> TokenUsage:
    fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    return TokenUsage(
        **{key: total([getattr(item, key) for item in items]) for key in fields},
        source=source,
        complete=bool(items) and all(item.complete for item in items),
        reported_requests=total([item.reported_requests for item in items]),
        models=sorted({model for item in items for model in item.models}),
    )


def claude_usage(stdout: str) -> TokenUsage:
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return TokenUsage()
    if not isinstance(data, dict) or data.get("type") != "result":
        return TokenUsage()
    models = data.get("modelUsage")
    if isinstance(models, dict) and models:
        items = []
        for model, raw in models.items():
            raw = raw if isinstance(raw, dict) else {}
            fresh, read, write, output = [
                count(raw.get(k))
                for k in (
                    "inputTokens",
                    "cacheReadInputTokens",
                    "cacheCreationInputTokens",
                    "outputTokens",
                )
            ]
            items.append(
                TokenUsage(
                    input_tokens=total([fresh, read, write]),
                    output_tokens=output,
                    cache_read_tokens=read,
                    cache_write_tokens=write,
                    complete=None not in (fresh, read, write, output),
                    models=[model],
                )
            )
        result = combine(items, "claude.result.modelUsage")
        # These are session aggregates, not individual HTTP request counts.
        return result
    raw = data.get("usage")
    if not isinstance(raw, dict):
        return TokenUsage()
    fresh, read, write, output = [
        count(raw.get(k))
        for k in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    ]
    return TokenUsage(
        input_tokens=total([fresh, read, write]),
        output_tokens=output,
        cache_read_tokens=read,
        cache_write_tokens=write,
        source="claude.result.usage",
        complete=None not in (fresh, read, write, output),
    )


def codex_usage(stdout: str) -> TokenUsage:
    items = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        raw = event.get("usage")
        raw = raw if isinstance(raw, dict) else {}
        inp, out = count(raw.get("input_tokens")), count(raw.get("output_tokens"))
        items.append(
            TokenUsage(
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=count(raw.get("cached_input_tokens")),
                cache_write_tokens=count(raw.get("cache_creation_input_tokens")),
                complete=inp is not None and out is not None,
            )
        )
    return combine(items, "codex.turn.completed")


def ollama_usage(raw: dict[str, Any]) -> TokenUsage:
    inp, out = count(raw.get("prompt_eval_count")), count(raw.get("eval_count"))
    return TokenUsage(
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=count(raw.get("prompt_eval_cached_count")),
        source="ollama.chat",
        reported_requests=1,
        complete=inp is not None and out is not None,
        models=[raw["model"]] if isinstance(raw.get("model"), str) else [],
    )
