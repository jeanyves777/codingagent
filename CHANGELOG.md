# Changelog

## 0.8.1 (post-pilot corrections)

Six corrections authorized after Round One of the Gauntlet pilot, each with targeted
regression tests in `tests/test_corrections.py`. `benchmarks/verify_corrections.py`
disables each correction in a copy and shows its tests fail without it.

1. **Completion verification.** Before implementing, the free model writes pytest
   requirement checks from the goal and the visible repository only (never hidden
   tests). After the visible tests pass, the checks run in a throwaway copy of the
   workspace; failures feed the repair loop within the normal failure budget and can
   trigger premium diagnosis. The checks are never committed. Broken checks are
   discarded. If they still fail when the budget is spent, the version that passed
   the visible tests is restored and reported as `completion_verified: false`.
   `BRAIN_REQUIREMENT_CHECKS` (default true); off for Gauntlet condition A.
2. **Escalation on proposal failures.** When every focused correction of the first
   proposal is rejected, the supervision policy may diagnose it, within the same
   per-task budget and ledger as test-failure escalation.
3. **Relevant knowledge retrieval.** Skills are matched on name and description, must
   share a term with the intent, and only one is taken per intent; reference passages
   stay out of the packet (available through `search_knowledge`/`read_skill`).
   Excerpts are capped at 700 characters; the default skill count is 2.
4. **Static analysis.** Proposed Python is checked with pyflakes for undefined names,
   and names imported from repository modules must exist there (the proposal's
   version of a module counts).
5. **Failure classification.** An upstream model failure (rejected proposals, spent
   attempts) is the root cause; later orchestration events are recorded as
   downstream effects. Reports score orchestration success and engineering success
   separately, and runs record their mode.
6. **Model-level accounting.** Each task keeps an `inference_log`: the model that
   actually served each inference (including server-side fallbacks and the models a
   CLI supervisor reports), tokens, routes, failovers between brains, and every
   escalation attempt. Gauntlet records include a per-run model summary.

## 0.7.0

- Added Phase 1 subscription connectors for the signed-in Claude Code and Codex CLIs:
  read-only, schema-constrained, API-key variables removed, API-key sign-ins refused.
- Added Phase 2 supervision: premium planning for complex goals and orchestrations,
  diagnosis after repeated free failures, optional takeover, per-task budgets, a
  daily cap, a call ledger, and human-granted escalation.
- Unreachable free models now pause a task (`awaiting_implementer`) and never escalate.
- Added Phase 3 GitHub fallback: publish verified work as a draft PR, read CI checks
  and reviews, and create follow-up tasks that update the same PR.
- Fixed API retry and cancel crashing without the durable queue (since 0.5).
- Small Ollama models: structured-output finalization when replies are prose or the
  round budget is spent; 600-second local timeout.
- Expanded validation from 60 to 73 passing tests.

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
