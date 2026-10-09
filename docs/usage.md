# Model usage and controlled comparisons

`dev-agent usage TICKET-ID` shows provider-reported input, output, cache read/write tokens,
call counts and durations, grouped by stage, role, provider and configured model.
Omit the ID for the project total. `--json` exports per-call records and grouped subtotals.
No model call is made by this command.

```sh
dev-agent usage EC2-CLEANUP-001
dev-agent usage EC2-CLEANUP-001 --json > /path/outside/project/baseline.json
```

Usage records live in `usage/*.json` under existing local ticket/run/plan artifact directories.
They are not committed. Each fresh call has a unique ID and RUNNING record written before
execution, followed by an atomic final record. Retries, fixes, plan-review corrections,
plan revisions and human revisions are separate calls. Resume does not overwrite earlier
records. A crash may leave RUNNING; this is not a successful or zero-token call.
Telemetry stores numbers, model names, timestamps, stage/role, paths, and a prompt hash;
it does not store prompt text or full provider responses. Failed calls additionally include
bounded, best-effort redacted error/stderr diagnostics; these are local troubleshooting
data and may contain paths or other provider-supplied text. Review before sharing.

## Accounting

- Input includes cache reads and writes. Cache columns are subsets; **do not add them again**.
- Claude uses the final `modelUsage` aggregate when available, otherwise final `usage`
  (subagents are disabled by this adapter). Fresh input + cache read + cache creation becomes
  total input. We do not sum both envelopes or per-message placeholder output counts.
- Codex enables JSONL, reads `turn.completed.usage` and retains the separate final-answer file.
  Codex input already includes cached input. Missing cache-write counts stay unknown.
- Ollama sums every `/api/chat` reply, including file-tool steps and structured report retries.
  `prompt_eval_count` and `eval_count` supply input/output; cache counts are optional.
- Missing/invalid metrics are `null`, displayed as unknown. Zero is retained only when reported.
  Coverage counts and `complete_calls` accompany totals. `*` means some calls lack that metric.
  A failed or timed-out call can have useful partial usage; missing final events cannot be recovered.
- Duration is measured for each agent invocation. Summing parallel investigations is **not**
  end-to-end workflow elapsed time. Test/lint/build processes do not contribute model tokens.
- SUCCEEDED means the provider call succeeded, not that subsequent artifact parsing, tests,
  review or the ticket succeeded. Check `dev-agent status` and validation artifacts too.
- This measures token workload, not invoices or subscription quota. Different model tokenizers,
  cache pricing and account plans prevent direct conversion into a trustworthy dollar saving.
- Calls completed before this feature have no recoverable measurements. A mixed old/new ticket
  report only covers recorded calls; it is not a retroactive full-ticket total.

Reported model names, where available, appear in the JSON usage `models` field. Configured
`default` is not an inferred model version. Unsupported metrics are not estimated from characters.

## Comparing the same batch

1. Choose fixed tickets and a fixed clean Git commit. Use independent source checkouts/worktrees
   for baseline and candidate so one implementation cannot contaminate the next run.
2. Pin provider/model and configuration; record CLI versions, repo SHA, request SHA and settings.
   Keep validation commands and acceptance criteria identical.
3. Run baseline and candidate multiple times, alternating order. Cache warmth and model latency
   vary; compare cache columns separately rather than treating one warm run as an algorithm gain.
4. Export `usage --json` for each ticket. Include failed attempts and all repairs, not just winners.
5. Compare per-stage input/output, calls, cache usage, coverage, success/validation outcomes and
   real start-to-finish elapsed time. Report medians/ranges across repeated runs. Do not compare
   complete totals with partial totals without explicitly noting missing usage.

This release instruments the existing workflow; it does not introduce RAG or fewer stages.
A repeated baseline experiment proves collection and illustrates variability, not optimization.

Field references: [Codex JSONL](https://learn.chatgpt.com/docs/non-interactive-mode),
[Claude usage](https://code.claude.com/docs/en/agent-sdk/cost-tracking),
[Ollama chat](https://docs.ollama.com/api/chat).

## Failure diagnostics

`dev-agent status TICKET-ID` shows the latest recorded provider failure for the current
run, its timestamp and role, the local artifact path and a conditional resume command.
This is historical: a later successful retry does not erase it. Existing runs without
diagnostics still work; missing past errors cannot be reconstructed.

Failed invocation records have a `diagnostic` object with `kind`, `error`, `stderr`
(when available), and a next-step hint. Kinds distinguish timeout, nonzero provider
exit, invalid provider response, launch exception and interruption. Claude result
`subtype` and explicit `errors` are preserved even on nonzero exit; full stdout,
answer text and tool transcripts are not copied. Unknown exceptions retain only their
type. Artifact schema errors and validation failures continue to use their existing
workflow reports; a successful provider call does not prove those checks passed.

Each error/stderr field is limited to 4,000 characters after best-effort credential
redaction. This is not a guarantee that arbitrary provider text contains no sensitive
data. Do not publish local diagnostic records without reviewing them. No automatic
retry or additional model call is introduced.
