# Cross-ticket project context

This optional first layer is a local project navigation index. It helps agents locate
project conventions, configuration and tests across tickets. It is not a semantic RAG
index or a shared conversation, and it makes no token-saving guarantee.

```yaml
project_context:
  enabled: true
  max_characters: 3000
```

Default is disabled; existing behavior and `parallel` investigation stay unchanged.
Set this before a new managed run: existing tickets keep their frozen configuration.
It works with parallel, explorer_first and single_pass, independently of same-ticket
`context.enabled`. It is separate from `knowledge.enabled`, which creates reviewed
knowledge documents alongside code.

## Content and use

The ignored local file `.dev-agent/project-context.json` contains:

- Top-level directory counts from tracked regular-file paths.
- Up to 500 candidate convention, configuration, README and test paths, ordered by
  path depth then name, with the number of omitted entries.
- Up to 12 AGENTS.md, CLAUDE.md, CONTRIBUTING.md or ARCHITECTURE.md documents (root
  before nested), with source hashes and up to 800 characters of excerpt each.
- Source HEAD, cache identity and whether Git reports local changes.

Documents larger than 32 KiB and symlinks are not read. Recognized sensitive/generated
paths are excluded. Excerpts use best-effort credential redaction; local project
instructions can still contain private information, so inspect before sharing.
The index contains source pointers, not inferred test commands or claims tests passed.
Agents must read applicable instructions and current source; excerpts are incomplete.

Ticket generation and investigation receive at most the configured character budget
(1500–12000, default 3000), with path categories represented before excerpts. Only a
small selection is sent, with simple path keyword matching. The normal task prompt,
independent review and validation remain intact. Later stages receive their existing
ticket evidence rather than another copy of this project index.

## Freshness

Before each eligible call the tool checks HEAD, the Git index and worktree status,
then rereads only the bounded convention documents and checks candidate path existence.
It does not scan code contents or ask a model to summarize the repository.
The derived body is compared with the saved cache, so corruption or local cache edits
are replaced rather than treated as authoritative project facts.

Commits, index changes, renames/deletions, tracked candidate disappearance and convention
content changes refresh the index on next use. Repeated edits to an already-dirty
convention document also refresh it. Arbitrary code-body edits may not change the map
because it stores only code paths, not code behavior. Nonignored untracked files affect
the dirty marker but are not indexed; commit/add relevant new files to index their paths.
Different worktrees keep separate local caches, avoiding evidence from another checkout.
Concurrent refreshes use unique temporary files and atomic replacement. This is a
point-in-time navigation aid, not a lock against simultaneous source edits.

## Inspect, refresh and measure

```sh
dev-agent project-context
dev-agent project-context --refresh
dev-agent usage TICKET-ID --json
```

The first two commands need no provider and work even when automatic injection is off.
Usage records expose `context.project`: enabled/used, characters, cache hit/refreshed
and identity. A missing, unreadable or unsafe cache falls back to normal investigation;
a corrupt regular cache is rebuilt when possible. No extra provider call is added.

The cache is reusable across tickets, but each injection still costs input tokens.
Use controlled full-ticket comparisons before claiming savings. This release verifies
reuse, invalidation and safety with automated tests; it does not claim measured savings
from real models. Reviewed requirements/experience extraction and embeddings are outside
this initial navigation layer.
