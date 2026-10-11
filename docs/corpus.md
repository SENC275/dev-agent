# Local corpus candidates

Collect a ticket from its original repository, after inspecting its result:

```sh
dev-agent corpus collect TICKET-ID
dev-agent corpus inspect TICKET-ID
```

This opt-in command makes no model calls, runs no validation commands, and changes
neither ticket state nor project code. Each collection creates a new local directory
under `.dev-agent/corpus/samples/TICKET-ID/`. These files use the existing local Git
exclusion. Nothing is uploaded or automatically used for training.

`bundle.json` contains the available requirement, plan, investigation reports,
validation/review evidence, workflow model/usage metadata, base commit, changed text
files before and after implementation, and their actual Git patch. `manifest.json`
records its checksum. `inspect` reads the latest sample and checks its checksum and
replays the patch in a temporary repository, comparing the resulting files and modes.
Saved patch replay does not require the original worktree to remain available.

## What the result proves

- `patch_replay: verified`: the patch reconstructs the saved changed files, including
  additions, deletions, executable modes and empty files. It does not prove that the
  complete application builds or that fresh tests pass.
- Validation and review are current only when existing evidence matches the collected
  source and approval. Missing, corrupt or stale evidence is reported as unknown.
- Human acceptance remains unknown at the human-review stage. A consistent recorded
  dev-agent merge receipt is reported as `recorded_merge`; a manual commit alone is
  not acceptance evidence. Post-merge historical checks may remain unknown.
- Every sample is `reconstructed`, not a complete captured execution trajectory.
  Unchanged repository files, dependencies, original provider inputs and tool traces
  are not captured. Workflow call metadata does not attribute the final patch to one
  specific model call.
- `training_eligible` is always false in this first version. Rights, sample quality,
  task splitting and train/evaluation leakage still need assessment.

## Limits

Collection conservatively excludes credential-like content, sensitive paths, binary
files, symlinks and oversized files. The detector can produce false positives and is
not a guarantee that every secret is identified; inspect candidates before sharing.
Code is never silently rewritten to redact it. If any changed file cannot be captured,
the entire patch is withheld and the gap is recorded. Evidence can also be withheld.

Limits: 200 changed files, 2 MB per file/artifact, 20 MB per bundle. Incomplete tickets
can produce partial candidates with explicit gaps. Repeated collection preserves older
samples. Checksums detect accidental modification, not malicious replacement of both
bundle and manifest. Automatic collection, labeling, dataset export and training are
future work.
