# Same-ticket context reuse

Enabled by default. This is a local, bounded evidence handoff, not RAG, shared model
memory, or a replacement for source inspection. It adds no model calls and skips no
workflow stages. It does not promise fewer tokens on every task.

```yaml
context:
  enabled: true
  max_characters: 4000
```

Set `enabled: false` **before a new run** for a baseline comparison. Existing run
configuration remains frozen. The budget bounds added context characters, not tokens
or the complete prompt. It can be set between 2000 and 32000.

## What is reused

- Ticket generation saves its existing current-behavior summary and an index of
  referenced tracked files in `.dev-agent/tickets/<ID>/context.json`. It does not make
  a second model call to summarize. The summary excerpt is limited to 1600 characters.
- Investigation reuses that seed only if ticket text, base commit and repository
  content match. Edited/external tickets without matching metadata use normal research.
- When all investigation roles succeed and source stayed unchanged, their findings
  become `.dev-agent/runs/<ID>/<run>/context.json`. This includes source artifact hashes
  and at most 24 referenced tracked file paths with content hashes.
- Implementer, reviewer, fixer and revise calls receive a bounded selection plus the
  file index. Implementation prioritizes entry points and patterns; review/fix prioritize
  risks and tests. This is deterministic selection, not a model-generated new verdict.
- Planner and plan reviewer already receive investigation artifacts, so nothing is
  added again. An implementer using the existing knowledge-candidate feature already
  receives investigation evidence, so it also avoids a duplicate package.

Before each use, the task's current worktree files are compared to the stored hashes.
Changed or missing files are flagged. Investigation findings remain **historical leads**;
unchanged file hashes do not prove that a claim is still correct or its dependencies
unchanged. Current diff, human feedback and validation continue through existing inputs.
Reviewers must independently verify source; implementer self-assessments are not shared
as proof. Agents can search beyond the index whenever necessary.

A changed ticket or changed research artifact invalidates reuse. Missing, damaged or
symlinked cache inputs fall back to normal prompts. No contents from another ticket
are looked up, and old runs without a context file remain usable. Context is local
and ignored by Git, separate from opt-in committed knowledge candidate documents.

`dev-agent usage <ID> --json` includes a `context` field for each call: whether it was
used, added character count, origin, and count of changed indexed files. Token counts
still come from the provider. Reusing text does not make it free model input.

For experiments use independent fixed snapshots, the same ticket/model/settings and
repeated alternating trials. Measure factual correctness and final validation alongside
input/output/cache tokens. A large shared package can increase consumption; use measured
results to adjust the budget. See [usage methodology](usage.md).

## Optional Explorer-first investigation

```yaml
context:
  enabled: true
  max_characters: 4000
  investigation_mode: explorer_first
```

The default remains `parallel`. `explorer_first` first runs Explorer, validates its
structured result and verifies that the repository snapshot has not changed. Pattern
and test research then run concurrently with bounded, file-indexed Explorer evidence.
Their prompts focus on missing specialist facts rather than repeating discovery;
source inspection and searching outside the index remain allowed. The final planner
still receives all three typed reports. No additional model call is introduced.

Failed Explorer output is not shared; specialists still investigate independently and
the overall investigation remains failed. Missing or stale evidence falls back to
normal lookup. All results, including a completed Explorer on cancellation, remain
in the local run. `context.enabled: false` restores parallel investigation as well.
The selected mode is stored in the investigation manifest and frozen run config.

Serializing Explorer can increase wall time. Reduced duplicate discovery is an intended
behavior, not a guaranteed reduction in tool reads, tokens, cost or elapsed time.
Compare multiple tickets with `parallel` versus `explorer_first`, keeping the rest of
the config fixed; include failures and cache usage. Do not compare this switch by
also disabling all downstream context reuse, which would change two variables.

An initial three-ticket full-workflow experiment found higher investigation input
in all three Explorer-first runs. All six workflows passed independent acceptance,
but no investigation-token reduction was demonstrated. This mode remains experimental
and off by default; use measured results rather than assuming a shorter handoff saves tokens.

## Optional single-pass investigation

```yaml
context:
  enabled: true
  investigation_mode: single_pass
  max_characters: 4000
```

One read-only agent invocation, using the `explorer` provider/model, returns an object
containing `exploration`, `patterns`, and `tests`. All three must validate before the
existing exploration.json, patterns.json, and tests.json contracts are published.
The combined response is retained as combined-investigation.json. The manifest records
that all three reports have one author; they are not independent investigations.
Usage records one real investigator invocation, not three synthetic role calls.
A CLI agent may still make multiple internal model requests/tool turns.

Pattern/test researcher role configurations remain valid for switching modes but are
not invoked in single_pass; doctor does not require their otherwise unused providers.
Planner, independent plan reviewer, implementation, validation, independent code reviewer
and fixer are unchanged. Invalid, incomplete, failed or timed-out combined responses
fail investigation without a hidden fallback or automatic extra call. A later explicit
resume follows the existing retry rules and records its added usage.

The default remains parallel. Apply this setting before a new run; existing runs keep
their frozen config. Disabling context also restores parallel. One investigator may
miss facts that specialists would catch, so compare evidence coverage and final quality
as well as token totals. This mode introduces no cross-ticket knowledge or RAG.

An initial three-ticket Claude full-workflow comparison found 17–42% lower total
input tokens with single_pass, with all six workflows passing independent functional
and scope checks. One ticket took longer and produced more output. These small mock
experiments did not control remote cache state or establish equivalent coverage for
large repositories, so parallel remains the default. When explicitly providing a
roles mapping, retain all seven role entries; single_pass only invokes explorer for
investigation and uses the other workflow roles as usual.
