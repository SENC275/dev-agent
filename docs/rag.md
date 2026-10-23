# Local project knowledge / RAG

RAG retrieves current source and documentation excerpts before each role call.
It complements the ticket's investigation reports and project path index; it does
not share a provider's hidden conversation state or remove independent review.

## Start with local retrieval

Add to `.dev-agent.yaml` **before starting a new ticket**:

```yaml
rag:
  enabled: true
  top_k: 6
  max_characters: 8000
```

Then, from the target project's Git root:

```bash
dev-agent rag index
dev-agent rag search "CloudWatch alarm missing data"
dev-agent dashboard
```

The Dashboard's Project knowledge section supports search and index refresh.
Manual index/search work even with `enabled: false`; `enabled` controls automatic
injection into agent prompts. Existing tickets keep their frozen configuration.

Search returns file paths, line ranges, SHA256 hashes, excerpts, retrieval mode,
and update counts. The per-call usage record includes RAG citations and overhead.
The default is SQLite FTS5 lexical retrieval, with identifier splitting and Chinese
bigrams. No embedding service is required. Retrieved text becomes part of the
prompt sent to the role's configured provider.

## Optional semantic retrieval

Configure an Ollama **embedding** model already installed on your server:

```yaml
rag:
  enabled: true
  embedding_url: http://127.0.0.1:11434
  embedding_model: your-installed-embedding-model
  embedding_timeout_seconds: 30
  max_embedding_chunks: 500
```

The model must support Ollama's `/api/embed` endpoint. Source snippets and queries
are sent to this configured endpoint; use a trusted server. Generation-only models
are not sufficient. See [Ollama embeddings documentation](https://github.com/ollama/ollama/blob/main/docs/capabilities/embeddings.mdx).

Search combines cosine similarity and lexical rankings using reciprocal-rank
fusion. Vectors are cached by endpoint, model, path and content. A repeated search
only embeds the query when source vectors are unchanged. If embedding fails or
exceeds the update budget, search falls back to lexical retrieval with a warning.
`rag index` updates text indexes only; the next search builds missing vectors.

After replacing a model under the same name, clear cached vectors:

```bash
dev-agent rag clear-vectors
```

This clears the vector cache for the Git project, including its worktrees, without
removing text indexes. The next search rebuilds vectors within the configured budget.

## Freshness and boundaries

- Each retrieval reads and hashes eligible current files. Only changed files are
  rechunked; deleted or excluded paths are removed. This reduces repeated model
  scanning, but still performs bounded local filesystem reads.
- The database is stored at `<git-common-dir>/dev-agent-rag.sqlite3`, outside tracked
  project files. Worktrees share storage and reusable vectors, but source mappings
  are isolated per checkout. No other branch's excerpts enter the current query.
- Before returning a hit, its source hash is checked again and the excerpt is
  reconstructed from the source. A file changed during retrieval is skipped.
- Git-ignored files, symlinks, binary files, common sensitive paths, lockfiles,
  state files and files larger than 256 KiB are excluded. Known credential patterns
  are redacted. This is not a guarantee that arbitrary sensitive source is detected.
- Defaults: at most 3,000 files / 20 MB scanned, 6 hits / 8,000 added characters.
  These limits are configurable. `omitted_files` reports files beyond scan budgets.
- Snippets are explicitly untrusted evidence. Agents must verify cited files before
  editing, and may inspect additional code when retrieval is incomplete.
- Knowledge committed as project documentation becomes searchable automatically.
  Unreviewed `.dev-agent` run artifacts are excluded from the project knowledge base.

This release tests semantic retrieval with a simulated embedding API. A live Ollama
embedding model should be verified in your environment before relying on that mode.
Token savings remain workload-dependent; compare the same tickets using usage logs.
