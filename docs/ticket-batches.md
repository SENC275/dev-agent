# Batch ticket drafting

Run from the target Git repository root, after configuring the planner provider:

```sh
dev-agent ticket batch --folder ~/requirements
```

Each immediate, non-hidden subfolder is one ticket. Its name is the ticket ID:

```text
requirements/
  TF-003/
    description.md
  TF-004/
    description.md
    constraints.md
    examples.txt
```

`description.md` is required and must be nonempty UTF-8. Other non-hidden `.md` and
`.txt` files in the same subfolder are included in filename order, with the description
first. Other file types and nested folder contents are not included. Symlink inputs
are rejected. IDs follow the existing 1–100 ASCII letter/digit/underscore/hyphen rule,
starting with a letter or digit; case-insensitive duplicates are invalid.
Limits: 100 ticket folders, 30 included files per ticket, 32 KB total description
including generated source headings. Keep requirement inputs outside the code repo.

The command uses the configured planner once per valid new ticket, sequentially.
Each planner still performs independent read-only repository research; this feature
is not a shared-research or token-saving mode. No workflow starts automatically.
Inspect `.dev-agent/tickets/TF-003.md`, resolve its open questions and then run:

```sh
dev-agent start TF-003 --file .dev-agent/tickets/TF-003.md
```

Review scope overlap and dependencies across drafts yourself. There is no automatic
cross-ticket dependency analysis or ordered implementation in this version.

## Repeating and recovery

Run the same batch command again. It prints per-ticket status and duration and a
local JSON report path under `.dev-agent/ticket-batches/`:

- `GENERATED`: new draft saved, with an input provenance receipt.
- `SKIPPED`: previous draft, input file hashes/path, configuration, Git base and code
  snapshot match the receipt. No new model call.
- `FAILED`: provider or generation failed. Other tickets continue. A later batch
  retries it if no output/receipt exists.
- `INVALID`: missing/invalid description or folder ID; correct the input and retry.
- `CONFLICT`: an existing draft/receipt does not match, is missing its counterpart,
  or cannot be verified. Nothing is overwritten. Inspect manually or use a new ID.

The exit status is 0 only when every item is generated or skipped; partial failures,
invalid entries and conflicts return 1. Reports and per-ticket usage stay local and
are excluded from Git by the existing local file rules. Source filenames/hashes,
configuration hash and repository fingerprint are recorded; configuration secrets
are not copied to the report. Original descriptions also appear in generated drafts.

If interrupted, completed receipts remain reusable. An output created just before a
crash may have no receipt; the next batch reports a conflict rather than overwriting
it. A report can retain a RUNNING item from the interrupted process. Batch locks are
released by the OS when the process exits. Source changes during a batch stop further
model calls with failures until you inspect and retry. There is no `--force` overwrite.

Existing single-ticket syntax remains supported, even for a ticket named `batch`:

```sh
dev-agent ticket TF-003 --file ~/request.md
dev-agent ticket batch --description "A single ticket actually named batch"
```

`--folder` is only valid with `ticket batch`, and cannot be combined with `--file`
or `--description`.
