# Dashboard workflow

From the target project:

```bash
dev-agent dashboard
# Several projects:
dev-agent dashboard --project ~/project-a --project ~/project-b
```

The server listens only on `127.0.0.1`. Open the full URL printed in the terminal;
its fragment contains the session token. Keep the server running while using it.
Use `--port 8766` to change port or `--no-open` to print the URL without opening it.

## Complete a ticket

1. **Initialize configuration**, then **Configure** providers, roles and commands.
   Run **Environment check**. Existing tickets retain frozen configuration; changing
   it may block resuming them, just as with the CLI.
2. **New ticket**: enter ID and requirement. Generate a draft with the configured
   planner, or save your own text. Both stay local under `.dev-agent/tickets/`.
3. Open the draft, edit and save it, then open its **Start preview** and confirm.
   Source code needs a clean committed baseline; local dev-agent files are excluded.
4. Open the ticket. At plan approval, inspect the plan, edit it directly, or use
   **R / Request changes** to ask the planner for revisions. Closing the dialog does
   not approve. Approval starts implementation and the configured checks/review.
5. Inspect current diff, validation, independent review and usage. Use **Resume**
   after a stopped workflow; explicitly add repair cycles if the previous limit
   was reached. Use **Revise** at human review to submit implementation feedback.
6. If the destination branch moved, **Sync latest baseline**. Resolve reported
   conflicts in the worktree and use **Continue conflict repair** when appropriate.
7. **Preview merge** shows the destination and current diff. Confirm to commit and
   merge using the existing workflow gates. This does not push or deploy.

Plan approvals, draft starts, configuration edits and merges are bound to the
content displayed in the preview. Changed content or expired previews require
review again. The Dashboard calls the same workflow functions as the CLI.

## Jobs and tests

Long operations run in background workers. The page polls status every three
seconds and displays an activity indicator, elapsed time, output and results.
One Dashboard mutation runs per project at a time; different projects can run
independently. This is not a ten-ticket automatic scheduler.

Job requests, output and receipts are saved locally under
`.dev-agent/dashboard/jobs/`. Job history shows the latest 20 attempts. On server
restart, an attempt without a completion receipt is marked **UNKNOWN** and is never
automatically replayed. Inspect ticket state before resuming; a worker may still be
running. Closing the browser does not cancel it; server shutdown waits for active
workers. Workflow locks continue to protect against concurrent CLI operations.

**Run test** executes the configured `commands.test` in the main project, not the
ticket worktree. It cannot replace workflow validation. This existing test-only
panel keeps its output in server memory and shows output after completion.

The RAG panel can search current code/documentation and refresh the index. See
[RAG setup and freshness](rag.md) for automatic role injection and embeddings.
