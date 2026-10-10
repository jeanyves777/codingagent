# Changelog

## Unreleased: typed engine API

See docs/engine-api.md.

- **`codingbrain api --describe | --stdio`:** a versioned (1.0) API that the desktop app and other
  programs use instead of parsing CLI output. It covers:
  - engine information and the schema;
  - doctor, install status and the install plan;
  - provider descriptors;
  - projects (list, register, create);
  - conversation, which only proposes;
  - tasks, with digest-bound, single-use approval, acceptance as a new branch, stop and resume;
  - activity-journal events streamed live while tasks run.
- **Provider registry (issue #14):** each provider reports separately whether it is installed,
  how it is authenticated, whether it is enabled and ready, how it is billed and its cost gate.
  Providers without a reviewed adapter (Gemini, Grok, Muse) are never reported as connected.

## Unreleased: conversational by default

See docs/assistant.md.

- **`codingbrain` from any folder starts a conversation.** It no longer queues every message as a
  coding task. In 0.9.0, typing `hello` started engineering work and went silent until Ctrl+C;
  now a greeting gets a greeting and a question gets an answer.
- **Intent routing.** Fixed rules handle the unmistakable cases: greetings, thanks, help,
  goodbye, slash commands and the project list. Everything else goes to the local model, which
  picks one of: chat, question, projects, status, investigate, plan, review, create_project or
  implement. The model only proposes; work needs an action verb and your confirmation. Without a
  model, uncertain messages are treated as conversation.
- **Global projects.** The assistant reads the project registry ("what projects am I working
  on?"). It resolves the project for a request from its name, the conversation, the current
  repository or matching file names, and asks when unclear. It never scans the home folder.
- **Safe task lifecycle.** Conversation never creates tasks, worktrees or files, and never spends
  premium budget. Ctrl+C while the model is thinking starts nothing. Tasks and conversations are
  listed separately.
- **Memory.** The global conversation log is redacted and tagged per project, and a conversation
  about one project never receives another project's turns.
- `codingbrain chat ["message"]`, and `/projects /tasks /history /run /new` inside the
  conversation. `codingbrain run "goal"` is unchanged.

## Unreleased: goal-first project creation

See docs/new-project.md.

- **`codingbrain new "<goal>"`** creates a new application from a sentence. It:
  1. checks readiness (nothing is created if Git, the model, Docker or the sandbox are missing);
  2. creates a new folder in your Projects folder, with path safety checks: it never reuses a
     folder, never overlaps Coding Brain's data, respects your allowed roots, and refuses nesting
     in other repositories and linked folders;
  3. runs `git init` and makes an empty baseline commit authored by you;
  4. makes a listed scaffold commit for the python, node or web stack;
  5. records the goal in project memory;
  6. builds and tests the goal through the normal lifecycle, with live activity and visual
     checks for web projects.
- **Git identity:** a missing identity is asked for and stored only in the new repository, never
  globally.
- **Dependencies:** stacks are dependency-free for the offline sandbox. Missing third-party
  packages are reported from the sandbox output, and network access is never granted quietly.

## Unreleased: complete installation and readiness

See docs/installation.md. This is a separate installer improvement on top of 0.12.0, waiting for
verification and approval.

- **`codingbrain install`** with Local and Full profiles. It runs a preflight, shows a plan, and
  asks permission before each change:
  - Python and Git;
  - WSL 2;
  - Docker Desktop;
  - Ollama;
  - a model chosen for this computer's memory;
  - Claude Code and Codex;
  - the sandbox images;
  - the knowledge library;
  - OCR, a vision model and a browser.

  Components come from winget or the vendor's official channel. Administrator rights are asked
  through UAC, signatures are recorded and tampered binaries are refused. No remote scripts are
  run and no security warnings are suppressed.
- **Restart-safe checkpoints:**
  - `--resume`;
  - an optional one-time resume after a WSL restart;
  - idempotent reruns;
  - one installation at a time.
- **`install.ps1 -Local` / `-Full` / `-Resume` / `-PlanOnly`:** prepares the environment before the
  `codingbrain` command is on PATH.
- **Readiness levels** replace "Healthy": Core, Sandbox, Hybrid and Full ready, Degraded, Blocked.
  - Levels count only verified evidence: a model that generated text, sandbox images that started
    offline, and a subscription sign-in.
  - `doctor --full` runs the real checks, and `doctor --json` includes `readiness`.
- **Virtualization** is judged from several signals. It is never reported as definitely disabled
  from Windows' firmware flag, and firmware settings are never changed.
- **Docker:** the installer separates the CLI from the engine and detects Windows-containers mode.
  It starts Docker Desktop and waits for it, and runs a sandbox smoke test with `--network none`.
- **Claude Code and Codex** have explicit sign-in states:
  - not installed;
  - not authenticated;
  - authenticated;
  - API-key billing (refused);
  - expired;
  - temporarily unavailable;
  - disabled.

  The official sign-in flows run without API-key variables, and no credentials are read or stored.
- **`codingbrain selftest`:** a disposable end-to-end task (a throwaway project, the real model and
  the sandbox).
- **`codingbrain setup --repair`** and **`codingbrain watch --install`**. Installer progress goes
  to the activity journal: tool-reported bytes and output, elapsed time and heartbeats. Percentages
  are never estimated.
- **Update, rollback and uninstall** never touch WSL, Docker, Ollama, models, Claude Code, Codex,
  Git or Python.

## 0.12.0

Live activity, execution traces and snapshots (see docs/live-activity.md).

- **Journal:** an append-only event journal in telemetry. Events are persisted as they happen,
  ordered, deduplicated for replayed milestones and redacted. Each event records the task and
  parent task, the agent, provider and model, the stage, a state (RUNNING, COMPLETED, FAILED,
  BLOCKED, RETRYING, WAITING_APPROVAL, CANCELLED), the duration, a summary and artifacts.
- **Instrumentation:** the existing service stages, from goal to acceptance and Git integration,
  report through the journal. Model calls report start, heartbeat and response for every
  provider, including the premium CLIs and vision models, with token counts where the provider
  gives them. The journal also records tool calls, routes, fallbacks, supervisor selection with
  remaining budgets, model-provided explanations and observed decisions.
- **Terminal:** `codingbrain run` shows live activity by default, with `--verbose`, `--quiet`,
  `--plain` and `--json`, and works on Windows PowerShell code pages and without a terminal.
  New commands: `activity`, `watch`, `trace`, `snapshots`, and
  `snapshot show|diff|restore|purge`.
- **Snapshots:** content-addressed and taken at each boundary. They include visual evidence
  from the multimodal verifier. Restoring always creates a new branch, after asking.
- **Includes the 0.9.1 fixes:** projects in the home folder, and read-only status commands.

## 0.11.0

Images, screenshots and documents, plus a visual development loop (see docs/multimodal.md).

- **Attachments:** `codingbrain run "goal" --attach FILE` (repeatable) accepts PNG, JPEG, WebP,
  GIF (frame selection), PDF, DOCX, XLSX, CSV, text, Markdown, JSON, YAML, XML and source files.
  - Formats are detected from content.
  - Files are parsed in an isolated process with time, size, pixel and decompression limits.
  - Links, secrets, programs, archives and legacy Office formats are refused.
  - Provenance (origin, SHA-256, page, cell, paragraph, frame) is kept, and macros, scripts and
    external links are never executed.
- **OCR:** local Tesseract for screenshots and scanned pages, with word boxes and confidence.
  Documents that contain real text are not OCRed.
- **Vision:** a separate vision model (Ollama, e.g. qwen2.5vl), used only when Ollama reports
  that it accepts images. OpenAI-compatible servers need an explicit `supports_images`
  declaration. Premium vision through the Claude/Codex CLIs needs approval per task and counts
  against the premium daily limit. Findings are structured and labelled as model judgments.
- **Routing:** parsers, OCR and vision run only for tasks with attachments. The coding model and
  the three-phase architecture are unchanged. The provider, model, time and tokens of every call
  are logged.
- **Visual development loop:** after tests pass, the frontend is built in the offline sandbox and
  served on 127.0.0.1 to a locked-down headless browser (Microsoft Edge on Windows). At desktop,
  tablet and mobile sizes it:
  - takes screenshots;
  - measures the layout (overflow, off-screen elements, overlapping controls, clipped text,
    broken images);
  - runs accessibility and keyboard checks;
  - collects console errors;
  - compares with the reference image, using both pixel measurements and the vision model.

  Blocking findings drive bounded repairs aimed at the likely components. Results are never
  declared pixel-perfect from model judgment.
- **Commands:**
  - `codingbrain attachments preview|list|show|approve|reprocess|purge`;
  - `codingbrain inspect-ui URL [--compare design.png]`;
  - `run --inspect-url URL` to capture the running app as evidence.
- **Memory:**
  - Requirements from documents, design references, diagrams and defect screenshots are stored
    as unverified records with provenance. Accepted design references and visual corrections are
    stored as verified history.
  - Design tokens are discovered from CSS variables, Tailwind and token files.
  - Re-extraction never overwrites approved records.
  - Retention is configurable, and `--sensitive` keeps only checksums.
- **Doctor:** reports coding, vision, OCR, document parsing, browser, Docker and premium
  readiness separately. Document parsing is part of the update health check.
- **Update:** `codingbrain update` from 0.9.0 and 0.10.0 keeps settings, memory and sessions and
  registers the new settings. Vision model weights are never downloaded without asking.
- **Fixes:**
  - Re-recording a memory record no longer downgrades an approved or verified record.
  - Loading settings no longer shares (and mutates) the built-in defaults.

## 0.10.0

Durable cross-agent project memory (see docs/project-memory.md).

- **Discovery:** project instruction files from Claude Code, Codex, Cursor, Copilot, OpenCode and
  others; READMEs, architecture docs, ADRs, plans, changelogs, Git history and Coding Brain's own
  work.
- **Private memory:** Claude Code and Codex sessions for the project and account-wide instruction
  files are imported only with authorization.
- **Three scopes:** global (approved rules only), per-project (isolated), and task/session.
  Versioned schemas are migrated and backed up by `codingbrain update`.
- **Records:** each keeps its provenance (source, file and line, times, checksum), a verification
  status and an authority level (1 to 6). Claims are reconciled with the code, and superseded and
  rejected decisions are tracked.
- **Safety:** conflicts are surfaced, not silently resolved; secrets are redacted before storage;
  suspected prompt injection is quarantined and kept out of model context.
- **Onboarding:** first launch prints a project profile and continuation summary; later launches
  sync incrementally. Accepted work and failed attempts are recorded automatically.
- **Commands:** `codingbrain memory scan|import|sync|status|show|conflicts|forget|approve|rule|contribute`.
- **Engine:** project memory reaches the model as provenance-labelled reference data in the
  existing engineering packet.

## 0.9.1

Fix: projects directly in the home folder (for example `C:\Users\me\my-app`) failed with
"Repository and data directories must be separate", because Coding Brain's data lives in
`C:\Users\me\AppData\Local\CodingBrain`.

- The safety boundary is now checked per project on resolved paths:
  - a project may sit next to Coding Brain's data, but never overlap it;
  - symbolic links, Windows junctions, different letter case and 8.3 short names are resolved
    before comparing;
  - the data directory can never be served as a project, and projects can never be inside it.
- A Git repository without commits gets clear instructions instead of a Git error. Coding Brain
  never commits your files for you.
- Opening a folder that overlaps Coding Brain's own data is refused with an explanation.
- `codingbrain status`, `tasks` and other read-only commands no longer mark a task that another
  terminal is running as interrupted; on startup only tasks whose process has ended are.
- Settings no longer share (and mutate) the built-in defaults.
- CI runs a home-folder project end to end in the real Docker sandbox on Linux, and adds Windows
  tests for junctions, case and short-name aliases.

## 0.9.0

Local installation for Windows (and Linux/macOS): install once, use `codingbrain` in any project,
update from verified GitHub releases.

- `codingbrain` command (`brain.local`):
  - **Commands:** interactive goals in the current project, plus `run`, `init`, `status`, `tasks`,
    `resume`, `accept`, `doctor`, `setup`, `update`, `rollback` and `--version`.
  - **Reuse:** it drives the existing service, so planning, orchestration, validation, sandboxed
    tests, repair, budgeted supervision, memory and approvals are unchanged.
  - **Branches:** accepted results become new `codingbrain/...` branches; the checked-out branch and
    working files are never modified.
- **Project recognition:** repository root, branch and status, languages, frameworks, dependency
  managers, docs, and existing build and test commands. Read-only. Each project has its own memory
  and sessions outside the project.
- **Settings:** set with `codingbrain setup` and stored outside the application. Covers local models
  (Ollama or OpenAI-compatible), Claude and Codex as actually signed in (API-key sign-ins refused),
  budgets (with a daily limit shared across projects), approval mode, allowed folders and the
  sandbox. No credentials are stored.
- **Updates:**
  - versions are installed side by side and downloads are checked against `SHA256SUMS`;
  - updates are refused during an active session or task;
  - the release's compatibility metadata is checked;
  - state is backed up, then migrated, then health-checked;
  - the launcher switches only on success, and a failure restores the backup;
  - `rollback` (optionally with `--restore-state`);
  - stable channel by default, with tagged pre-releases on the dev channel.
- **Windows packaging:** `install.ps1` (checksum-verified, user scope, PATH) and `uninstall.ps1`
  (keeps data unless `-RemoveData`; never touches projects).
- **Release tooling:** `scripts/build_release.py` (wheel, Windows-resolved constraints,
  `release.json`, `SHA256SUMS`), a Windows CI workflow that runs `installer/windows/verify.ps1`, and
  tag-triggered publishing with build provenance.
- **Migration from earlier versions:** source checkouts keep working as before. To use the local
  CLI, install 0.9.0 with `install.ps1`; existing `brain-data` directories are not imported
  automatically.

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
