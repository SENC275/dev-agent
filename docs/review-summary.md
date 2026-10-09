# Human review summary

```sh
dev-agent summary TICKET-ID
dev-agent summary TICKET-ID --json
```

Run from the source repository. This read-only overview does not call agents, execute
validation commands, approve a plan, change the ticket state, or merge code. Start/resume
and successful revise output a shortcut to the summary command.

The overview contains:

- Current worktree changes relative to the recorded base, including staged, unstaged
  and untracked files. Renames are shown as delete/add. The terminal lists 30 files;
  JSON contains all entries. `git diff` does not include untracked file contents, so
  inspect those separately using the displayed file list and `git status`.
- The recorded test/lint/typecheck commands, status and exit codes. Code fingerprint,
  base commit, worktree and plan approval are checked for freshness. A historical
  PASSED record with changed code is marked STALE/unverified.
- Latest recorded review findings, with severity, location, scenario and recommended
  action. A clear review of unchanged code remains current after final revalidation;
  it need not reference the latest validation run ID. Findings are compared with their
  archived copy. Missing/corrupt records are unavailable, never an inferred zero.
  The terminal shows ten findings; JSON contains all recorded findings and impact.
- Cumulative recorded token input/output/cache counts for the ticket, including failed
  calls and prior attempts. Missing metrics stay unknown; partial totals are labeled
  known subtotals. Input already includes cache. Calls before metering cannot be counted.
- Safely quoted diff/status commands and next actions. Only ready tickets get revise
  and merge-preview hints. The actual merge command is offered only when existing
  merge preflight checks pass; human acceptance is still required.

The summary shows persisted state alongside current checks. It does not silently
repair stale artifacts or retry failed work. Read warnings before taking an action.
A missing worktree, unreadable artifact or old unmetered run can still yield a partial
summary. JSON exposes warnings and current flags for automation. Merge performs its
own checks again; this point-in-time overview is not an approval or a lock against
concurrent edits. CLI command lines and artifact contents are local project data;
review exported summaries before sharing.
