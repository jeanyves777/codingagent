# Changelog

## 0.6.0

- Added multiple free brains: an OpenAI-compatible provider (LM Studio, llama.cpp,
  vLLM, LocalAI, Jan, free hosted tiers) alongside the default Ollama provider, and a
  `BRAIN_BRAINS_CONFIG` file assigning ordered failover chains to each role.
- Added an optional Claude provider (`.[claude]` extra) with tool use, refusal
  handling, and server-side refusal fallbacks enabled by default.
- Added immediate sandbox cancellation: a running test container is removed when
  cancellation is requested.
- Added call-graph extraction and caller/callee context for goal-relevant symbols.
- Added workspace cleanup and age-based pruning for finished tasks and
  orchestrations, pinning accepted commits under `refs/coding-brain/`.
- Added Linux/macOS setup commands.
- Expanded validation from 46 to 60 passing tests.

## 0.5.0

- Added persistent approval-required MCP tool requests with safe task pause/resume.
- Added at-most-once side-effect execution and durable result replay.
- Added append-only runtime events with independent consumer cursors.
- Added event-driven orchestration progression through durable queue jobs.
- Added trace propagation and nested spans across API, workers, and model calls.
- Added adaptive model routing trained only by verified task outcomes.
- Expanded validation from 40 to 46 passing tests.

## 0.4.0

- Added an allowlisted MCP client gateway for autonomous read-only capabilities.
- Added explicit `read_only` and `approval_required` policies; mutating MCP tools
  are never exposed to the model.
- Added a durable SQLite queue with atomic claims, deduplication, worker leases,
  heartbeats, cancellation, and fail-closed lease expiry.
- Added standalone worker processes and queue visibility through the API.
- Added executable benchmark suites for scope, review, and test regression scoring.
- Expanded validation from 32 to 40 passing tests.

## 0.3.0

- Added verified semantic memory with Ollama embeddings.
- Added SQLite vector and PostgreSQL/pgvector storage.
- Added memory classifications, deferred writes, and resynchronization.
- Added fast/strong model routing and generation budgets.
- Added a deny-by-default typed capability registry.
- Added Python and Node sandbox profiles.
- Added evidence metrics and verified JSONL learning export.
- Expanded validation from 23 to 32 passing tests.

## 0.2.0

- Added Tree-sitter structural repository indexing.
- Added validated dependency graphs and dependent-task scheduling.
- Added Git worktree task isolation and staged orchestration integration.
- Added separate coordinator, implementer, and reviewer model selection.
- Added conflict reporting, cooperative cancellation, retry, restart detection,
  event streaming, repository-context inspection, and commit-linked memory.
- Expanded validation from 17 to 23 passing tests.

## 0.1.0

- Added the initial authenticated backend, Ollama tool loop, filtered snapshots,
  isolated pytest runner, concurrent workers, basic delegation, reviewer role,
  approval digests, and accepted-memory storage.
