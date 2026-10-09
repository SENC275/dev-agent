# Resume recovery

Run `dev-agent resume TICKET-ID` from the source repository with the run's original
configuration. This command continues the saved ticket; it does not reset fix budgets.

Before any worktree creation attempt, recovery checks the Git HEAD and a fingerprint
of tracked and nonignored source contents, plus hashes of the investigation reports.
These checks are independent of context reuse and do not call a model.

- Unchanged inputs: completed stages are reused. An interrupted planner or failed plan
  reviewer retries at that stage without repeating successful investigation.
- Source HEAD/content changed, or research reports changed/disappeared: a clean source
  baseline starts a fresh investigation and plan under the same ticket. New approval
  is required through the configured human/agent gate.
- Only the plan or its metadata is missing/invalid: verified research is retained;
  planning and approval run again. Valid human edits to plan.md remain supported.
- Dirty source: commit or stash it yourself, then resume. The tool does not overwrite,
  commit or stash source changes on your behalf.
- Old runs without a verifiable checkpoint: before worktree creation, conservatively
  regenerate read-only stages once. Existing worktrees use their existing recovery rules.

A recovery marker separates active steps from historical steps. Old artifacts, plans,
failures and token records remain available; repeated resume does not keep replaying
successfully refreshed stages. Checkpoints are local ignored `.dev-agent/recovery-*.json`
files containing fingerprints, not source text. They are not a shared project knowledge cache.

Once worktree creation has been attempted, automatic read-only reset is disabled:
there may already be side effects even if the journal has no successful receipt.
Interrupted implementation, validation commands, revisions and fixes retain the existing
conservative inspection rules. This change does not automatically replay uncertain writes
or partial shell commands. Completed validation/fix recovery and extra fix-cycle grants
continue through their existing checked paths.

Recovery granularity is a workflow stage: a failed investigation reruns the investigation,
not only its failed specialist. No retries are added inside an individual provider call.
