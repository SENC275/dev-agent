# Integrate a ticket after the source branch advances

Separate terminals can run different ticket IDs in separate worktrees. Start with
independent requirements and isolated tests. Worktrees do not isolate ports, Docker
containers, databases, Terraform state, or provider quotas.

There is no ten-ticket scheduler or automatic merge queue yet. This first version
supports sequential integration after parallel implementation.

## Two tickets from the same baseline

After both tickets reach human review, accept the first from the source repository:

```sh
dev-agent merge TF-001 --dry-run
dev-agent merge TF-001
```

The second ticket still targets the old baseline. Update it explicitly:

```sh
dev-agent sync TF-002
dev-agent summary TF-002
dev-agent merge TF-002 --dry-run
dev-agent merge TF-002
```

`sync` does not merge into the source branch, push or deploy. Final merge retains the
usual human confirmation. Requires Git 2.38+ for `merge-tree --write-tree`, a clean
source checkout, and a previously validated/reviewed ticket with unchanged, unstaged
worktree changes. Manually committed worktrees are not supported by this first version.
The latest source HEAD must descend from the ticket's original base.

## What sync does

1. Checks the old code against its saved passing validation and clear review.
2. Preserves its contents as a Git object under `refs/dev-agent/sync/…`, using a
   separate index. The original worktree, branch and index remain unchanged.
3. Computes a three-way integration with the latest source using Git merge-tree.
4. Investigates the latest source and generates a new plan, including the original
   requirement, accepted revision feedback, old plan, ticket diff and upstream diff.
5. Uses the configured plan gate again: agent review or interactive human approval.
6. Creates a new worktree/branch with a `-sync-<id>` suffix and applies the integration.
7. If there are conflicts, calls the configured fixer once, unless automatic fixing
   is disabled by `limits.max_fix_cycles: 0`.
8. Runs fresh configured validation, independent review and the normal bounded fix
   cycle. Old validation and review results never count for the new baseline.

Fresh investigation and review consume provider calls even when Git reports no
conflict. No text conflict is not proof of behavioral compatibility. The one conflict
resolution attempt is separate from the new integration plan's normal fix budget.
Previously accepted upstream behavior and the ticket's intent must both be checked.
The updated plan may still need a follow-up revision if new implementation work is
required beyond mechanically combining the existing changes.

Inspect the new `worktree_path` from `dev-agent status TF-002`; it differs from the old
worktree. Old worktrees and records are retained for comparison, not auto-deleted.
Keep them until you are satisfied with the integrated result.

## Human decisions and recovery

If the conflict fixer fails, requests a human decision, returns an invalid decision,
or leaves conflict markers, inspect its summary and the
conflict list in `.dev-agent/sync/TF-002/active.json`. Edit the **new integration
worktree**, leaving changes unstaged and uncommitted, then explicitly continue:

```sh
dev-agent sync TF-002 --continue
```

This does not repeat the conflict fixer. It checks remaining markers, verifies source
and worktree identity, and runs validation/review again. A second fixer may still run
later for independently reported validation or review failures under the normal budget.
No software check can prove a semantic conflict is resolved; review the final diff.
Symlink conflicts are conservatively left for manual Git handling in this version.

For a completed integration fix cycle that exhausts its budget, use:

```sh
dev-agent resume TF-002 --additional-fix-cycles 1
```

While awaiting plan approval, `resume` reopens the approval flow and continues sync.
Configuration must still match the ticket's original frozen YAML. Do not change the
source branch, source HEAD or old worktree during integration, including while waiting
for approval. Source movement is detected and stops the attempt.

Other interrupted/failed integration stages are deliberately not replayed, including
uncertain worktree creation, provider writes and validation commands. Inspect
`active.json`, its attempt directory and both worktrees; a fresh ticket or manual Git
recovery may be needed. `--continue` is only available for a recorded conflict pause,
not as a general override for incomplete attempts.

A project-wide `merge.lock` serializes sync and merge. Normal completion releases it;
a killed process may leave it behind. Confirm no process is running and inspect the
attempt before removing a stale lock. This lock coordinates dev-agent processes,
not unrelated manual Git operations. Other tickets may continue their isolated work,
but source changes can invalidate their evidence and require their own sync later.
