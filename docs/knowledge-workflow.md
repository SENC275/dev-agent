# Knowledge candidates

Enable `knowledge.enabled: true` in `.dev-agent.yaml` before starting a new ticket.
New `dev-agent init` configurations include it; old configurations without the field
remain disabled. Do not change configuration midway through a managed run.

## Lifecycle

1. Investigation reads relevant existing knowledge as leads and rechecks current code.
2. Planning includes the candidate document in its scope when enabled.
3. Implementation receives the investigation results and drafts up to five candidates
   in `docs/knowledge/<ticket>.md`, alongside the code and before validation.
4. Independent review checks both code and knowledge. The reviewer does not inherit
   the implementer's conversation or summary. Mechanical checks also report missing
   documents, invalid metadata, missing fields, and invalid source references.
5. Fixes and revisions must keep knowledge consistent with the resulting code.
6. Human merge acceptance includes the document in the same commit as the code.
   There is no extra approval gate or dedicated model call for knowledge generation.

Candidates are observations to review, not automatic edits to `AGENTS.md` or
`CLAUDE.md`, and never override project instructions. The system does not certify
semantic truth: source existence checks are supplemented by model and human review.
Existing committed knowledge is only a lead on later tasks, not permanent truth.

## Document format

The implementation receives exact ticket/base metadata and a template. Keep the
English labels; free-text content can use the ticket's language. For example:

```markdown
# Knowledge candidates

Ticket: DEMO-001
Base commit: <actual base commit supplied by the workflow>

## Candidate: Task listing order

- Kind: fact
- Claim: Task listing sorts by ID before pagination.
- Scope: GET /tasks in this implementation.
- Evidence: app/main.py:42
- Recheck: Recheck if ordering, filtering or pagination changes.
```

Use `fact`, `convention`, `decision`, or `pitfall` for Kind. Evidence must name one
existing, nonignored repository-relative file and line (or inclusive line range,
e.g. `app/main.py:42-45`) in the current worktree;
it cannot refer to another generated knowledge document. Use actual source references,
not the illustrative path above. At most five candidates and 32 KB are allowed.

The base commit identifies the starting point, not a claim that the new code existed
at that commit. Review receipts bind the candidate and source files to the final
worktree fingerprint; Git history records the accepted version at merge.

No reusable knowledge is also a valid result. Keep the same metadata, followed by:

```markdown
## No candidates

This task introduces no new reusable knowledge beyond existing project documentation.
```

The reviewer should check that this reason is justified. Do not invent rules to fill
a quota or turn a one-ticket requirement into a universal convention.

## Human review

Read the candidate file in the worktree, including new files that ordinary `git diff`
does not display. Use `dev-agent revise <ticket>` to correct entries or say, for example:

> Remove the migration convention candidate; it only applied to this ticket.

To reject all entries, ask for `No candidates` with your reason. Removing the entire
file while this feature is enabled creates a missing-document finding. Changes after
review invalidate the existing snapshot and must be validated and reviewed again.
The merge preview reminds you that you are accepting knowledge together with code.

This version does not automatically rewrite older knowledge or resolve contradictions
across the whole repository. Keep entries small and review their applicability before reuse.
