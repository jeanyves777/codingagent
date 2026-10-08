# Coding Brain v0.5 — resumable agents and observable workflows

Coding Brain is a local, single-user coding-agent backend that owns orchestration,
repository context, accepted memory, isolated execution, review, testing, and Git
integration. Models and external tool servers are replaceable. This release is an agent-runtime foundation,
not a trained model or a production multi-tenant service.

## What v0.5 adds

- Persistent approval requests for side-effecting MCP tools.
- Safe pause and resume from the existing worktree after approval.
- At-most-once approved tool execution with durable result replay.
- An append-only event stream with named consumer cursors.
- Event-driven dependency progression through durable queue jobs.
- Trace IDs and nested spans propagated across API, queue, worker, and model calls.
- Adaptive fast/strong selection learned only from verified outcomes.

## Retained from v0.4

- A durable SQLite job queue with atomic claims, deduplication, leases, and heartbeats.
- Multiple standalone worker processes for concurrent planning and execution.
- Fail-closed recovery: an expired running job is blocked for human retry, never replayed blindly.
- An MCP client gateway with an explicit per-tool policy allowlist.
- Autonomous access to tools marked `read_only`; side effects require explicit approval.
- Executable benchmark suites that score proposed scope, review, and tests.

## Retained from v0.3

- Verified semantic memory through Ollama embeddings.
- Local SQLite vectors or PostgreSQL/pgvector with HNSW cosine search.
- Episodic, semantic, and procedural memory classifications.
- Deferred memory writes and explicit recovery synchronization.
- Deterministic fast/strong model routing with generation budgets.
- A deny-by-default typed capability registry.
- Python and Node sandbox profiles with model-inaccessible test configuration.
- Evidence metrics and verified JSONL learning-data export.

## Retained from v0.2

- Tree-sitter indexing for Python, JavaScript, TypeScript, and TSX.
- Symbols, imports, and parse errors supplied to the implementing model.
- Dependency-aware task graphs with up to six assignments.
- Parallel execution for independent assignments.
- A detached Git worktree for every Git-backed coding task.
- An orchestration integration worktree with conflict-safe cherry-picking.
- Downstream tasks start from the accepted upstream integration commit.
- Separate coordinator, implementer, and reviewer model configuration.
- Docker-isolated Python testing and bounded repair attempts.
- Cooperative cancellation at safe orchestration boundaries.
- Retry from a clean workspace after blocked, failed, cancelled, or conflicted work.
- Server-sent task events, persistent task history, and restart detection.
- Accepted, repository-scoped memory with test evidence and commit linkage.

Dependency orchestration follows this sequence: user goal, coordinator, validated
dependency graph, parallel root agents, independent review, isolated tests,
human acceptance, integration worktree, then newly unblocked dependent agents.

## Important behavior

The original repository working tree is never edited. Each task runs in a
detached worktree. A test-passed task becomes a commit only after human
acceptance. For an orchestration, that commit is cherry-picked into its
integration worktree. If Git reports a conflict, the orchestration stops with
integration_conflict; it never guesses a resolution.

Dependency orchestration requires a Git repository with no tracked local
changes. Standalone tasks may use either Git worktrees or filtered snapshots.
Untracked files are not included in Git worktrees. Commit or intentionally add
required project files before submitting work.

## Requirements

- Python 3.11 or newer
- Git
- Ollama with one or more installed tool-capable models
- Docker Desktop using Linux containers
- Windows PowerShell examples below

## Install and start

Extract the archive and open PowerShell in the coding-brain folder:

    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
    New-Item -ItemType Directory -Force repositories | Out-Null
    $env:BRAIN_API_TOKEN = (.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))")
    $env:BRAIN_MODEL = "YOUR_INSTALLED_IMPLEMENTER_MODEL"
    $env:BRAIN_COORDINATOR_MODEL = $env:BRAIN_MODEL
    $env:BRAIN_REVIEW_MODEL = $env:BRAIN_MODEL
    $env:BRAIN_WORKERS = "3"
    $env:BRAIN_DURABLE_QUEUE = "true"
    docker build -f Dockerfile.sandbox -t coding-brain-sandbox:0.1 .
    docker build -f Dockerfile.sandbox.node -t coding-brain-node-sandbox:0.1 .
    .\.venv\Scripts\python.exe -m pytest -q
    .\.venv\Scripts\python.exe -m uvicorn brain.api:create_app --factory --host 127.0.0.1 --port 8000
    .\.venv\Scripts\python.exe -m brain.worker

Keep the API bound to 127.0.0.1. Set the same environment in every worker
terminal. Use one Uvicorn process and start additional `brain.worker` processes
to increase concurrency. SQLite atomically assigns each queued job to one worker.

Put a trusted Git repository under repositories, for example:

    coding-brain/
      repositories/
        demo/
          .git/
          ...

## Standalone coding task

    .\.venv\Scripts\python.exe client.py submit --repository demo --goal "Fix the authentication bug without changing unrelated behavior"
    .\.venv\Scripts\python.exe client.py status --id TASK_ID
    .\.venv\Scripts\python.exe client.py events --id TASK_ID

When status becomes proposed, inspect plan, changes, and diff. Approve the exact
immutable proposal digest:

    .\.venv\Scripts\python.exe client.py execute --id TASK_ID --digest PROPOSAL_DIGEST
    .\.venv\Scripts\python.exe client.py status --id TASK_ID
    .\.venv\Scripts\python.exe client.py accept --id TASK_ID --summary "Verified authentication fix and lesson learned"

Execution performs independent review, applies the proposal inside the task
worktree, runs isolated tests, and allows up to two model repair attempts.
Acceptance is rejected unless tests passed.

## Dependency-aware orchestration

    .\.venv\Scripts\python.exe client.py delegate --repository demo --goal "Add a user endpoint, tests, and documentation with the correct dependencies"
    .\.venv\Scripts\python.exe client.py group --id ORCHESTRATION_ID

The coordinator returns assignments with depends_on. Root assignments are
planned concurrently. Each proposed child still requires its own digest approval
and acceptance. After an upstream child is accepted, the coordinator integrates
its commit and automatically starts newly unblocked dependents from that commit.

The group response includes child states, integration history, the current
integration_head, and proposed overlapping paths. Completed work remains on the
integration commit; the original checked-out branch remains unchanged.

Review before bringing it into the original repository:

    git -C repositories\demo diff BASE_COMMIT..INTEGRATION_HEAD
    git -C repositories\demo merge --ff-only INTEGRATION_HEAD

Only run the merge after inspecting the diff and confirming the original branch
still points at the orchestration base. If it has moved, use your normal reviewed
Git integration workflow.

## Cancellation and recovery

    .\.venv\Scripts\python.exe client.py cancel --id TASK_OR_ORCHESTRATION_ID
    .\.venv\Scripts\python.exe client.py retry --id FAILED_TASK_ID

Cancellation is cooperative. A running model request or Docker test is allowed
to reach the next safe boundary. Restarted in-flight tasks are marked blocked
instead of being silently resumed. Retry creates a fresh workspace from the
current valid base. Orchestrations themselves cannot yet be retried; retry the
blocked child or create a new orchestration.

With the durable queue enabled, queued jobs survive API and worker restarts.
Workers renew a lease while operating. An expired lease is marked failed and its
task becomes blocked when the API reconciles the queue. This avoids automatically
replaying an operation that may already have changed a worktree.

## MCP tool gateway

Copy `mcp.example.json`, set each server URL, and explicitly classify every tool:

    $env:BRAIN_MCP_CONFIG = "mcp.json"
    .\.venv\Scripts\python.exe client.py mcp-tools

Tools marked `read_only` run autonomously. A call to an `approval_required` tool
pauses the task before the side effect and creates a durable request:

    .\.venv\Scripts\python.exe client.py approvals --id TASK_ID
    .\.venv\Scripts\python.exe client.py approve-tool --id REQUEST_ID

Use `deny-tool` to reject it. Approval resumes planning from the preserved
worktree. The approved call is consumed before network execution, so a worker
crash cannot silently replay a side effect. Successful results are stored and
replayed to the resumed model loop without executing the tool twice. Remote
servers require HTTPS; plain HTTP is accepted only for loopback addresses.
Credentials must not be embedded in MCP URLs.

## Events and traces

Every runtime transition is appended to a durable event stream. Named consumers
retain independent cursors, allowing workflow rules to resume after restarts.
Terminal child events schedule idempotent parent-graph reevaluation.

    .\.venv\Scripts\python.exe client.py global-events
    .\.venv\Scripts\python.exe client.py trace --id TRACE_ID

Task responses include `trace_id`. Queue jobs and nested model spans keep that
identifier across worker processes.

## Executable benchmarks

Edit `benchmarks/example.json` to reference trusted local repositories, then run:

    .\.venv\Scripts\python.exe -m brain.benchmark benchmarks/example.json
    .\.venv\Scripts\python.exe -m brain.benchmark benchmarks/example.json --execute --output benchmark-result.json

The default mode plans only and scores expected-path recall plus allowed-scope
precision. `--execute` proceeds only when both scope scores are perfect, then
adds independent review and isolated test results. Benchmark runs do not accept
changes or write verified memory.

## Repository intelligence and memory

    .\.venv\Scripts\python.exe client.py context --repository demo --goal "authentication token"
    .\.venv\Scripts\python.exe client.py memory --repository demo

The structural index extracts named declarations and imports. Relevant entries
are supplied to the implementing model before it edits. Indexing is structural,
not yet semantic embedding search.

Enable semantic memory with an installed embedding model:

    ollama pull embeddinggemma
    $env:BRAIN_EMBEDDING_MODEL = "embeddinggemma"
    $env:BRAIN_EMBEDDING_DIMENSIONS = "768"

Without BRAIN_POSTGRES_DSN, verified vectors are stored locally in
brain-data/semantic-memory.sqlite3. Search them with:

    .\.venv\Scripts\python.exe client.py semantic-memory --repository demo --goal "token rotation"

For PostgreSQL/pgvector, create a private password file and start the supplied service:

    New-Item -ItemType Directory -Force secrets | Out-Null
    [System.IO.File]::WriteAllText("secrets/postgres_password.txt", "REPLACE_WITH_A_LONG_RANDOM_PASSWORD")
    docker compose -f compose.memory.yaml up -d
    $env:BRAIN_POSTGRES_DSN = "postgresql://coding_brain:URL_ENCODED_PASSWORD@127.0.0.1:5432/coding_brain"

The password file is ignored by Git. Do not commit or print it. If embeddings
were unavailable during acceptance, synchronize the accepted task later:

    .\.venv\Scripts\python.exe client.py sync-memory --id ACCEPTED_TASK_ID

Memory contains only human-accepted, test-passed work. Semantic retrieval is
cosine-ranked and repository-scoped. The original keyword audit remains available.

## Model routing and budgets

Set BRAIN_FAST_MODEL to a smaller installed coding model. Routing begins with
the deterministic complexity policy. After both models have at least five
verified outcomes, smoothed success rates may change the selection while still
favoring the strong model for complex work. Only accepted tasks count as
success; failed verified executions count as failures. Inspect decisions with:

    .\.venv\Scripts\python.exe client.py routes

The report includes the route strategy and per-model verified performance.

## Python and Node testing

A package.json-only project selects the Node profile. A repository owner may add
coding-brain.json containing, for example:

    {"test_profile":"node","test_command":["npm","run","test:unit"]}

The command is an argument array, its executable must be allowlisted, and no
shell is used. The default Node image has no project dependencies. Build a
trusted image with dependencies under /opt/node_modules and set
BRAIN_NODE_SANDBOX_IMAGE.

## Evaluation and verified learning data

    .\.venv\Scripts\python.exe client.py evaluation
    .\.venv\Scripts\python.exe client.py learning > verified-learning.jsonl

Learning exports contain only accepted tasks whose tests passed. They include
source replacements and diffs, so protect exports like the repository itself.

## Configuration

| Environment variable | Default or requirement |
| --- | --- |
| BRAIN_API_TOKEN | Required; random string of at least 32 characters |
| BRAIN_MODEL | Required; installed Ollama implementer model |
| BRAIN_FAST_MODEL | Defaults to BRAIN_MODEL; optional smaller implementer |
| BRAIN_COORDINATOR_MODEL | Defaults to BRAIN_MODEL |
| BRAIN_REVIEW_MODEL | Defaults to BRAIN_MODEL |
| BRAIN_MODEL_URL | http://localhost:11434 |
| BRAIN_REPOSITORIES | repositories |
| BRAIN_DATA | brain-data, outside the repository root |
| BRAIN_WORKERS | 3, clamped to 1–8 |
| BRAIN_DURABLE_QUEUE | false; enable the persistent worker queue |
| BRAIN_MCP_CONFIG | Optional path to an allowlisted MCP JSON configuration |
| BRAIN_SANDBOX_IMAGE | coding-brain-sandbox:0.1 |
| BRAIN_NODE_SANDBOX_IMAGE | coding-brain-node-sandbox:0.1 |
| BRAIN_EMBEDDING_MODEL | Optional; enables semantic memory |
| BRAIN_EMBEDDING_DIMENSIONS | 768; fixed for an existing vector store |
| BRAIN_POSTGRES_DSN | Optional; selects PostgreSQL/pgvector |
| BRAIN_STRONG_THRESHOLD | 4; routing complexity threshold |
| BRAIN_MAX_TOOL_ROUNDS | 8, clamped to 1–16 |
| BRAIN_MAX_OUTPUT_TOKENS | 8192, clamped to 256–32768 |
| BRAIN_MAX_CONTEXT_CHARS | 200000, clamped to 10000–1000000 |
| BRAIN_API_URL | Client URL; http://127.0.0.1:8000 |

## Security boundaries

The Docker runner uses no network, a read-only source mount, no Docker socket,
no added capabilities, no-new-privileges, CPU/memory/process limits, a non-root
user, and a 120-second timeout. A container is not a virtual-machine security
boundary. Use only trusted repositories and prebuilt sandbox images.

Snapshots exclude hidden paths, symlinks, common credential names, dependencies,
build output, and Git metadata. Limits are 1,000 files, 200 KB per file, and
20 MB total. Credential-name filtering is not comprehensive. Repository source
is sent to the configured model endpoint.

The model cannot choose arbitrary shell commands. It can list, search, and read
allowed source files, then propose at most ten complete replacement files. Tests
execute a fixed command. Dependency installation does not occur during a task.

## Validation

The release passed 46 automated tests covering path and data restrictions,
Tree-sitter Python and TypeScript indexing, graph cycle rejection, proposal
approval, memory gating, worker limits, repository isolation, real worktree
commits, dependency inheritance, downstream blocking, integration conflicts,
cancellation, retry, restart handling, authentication, events, repository
context, sandbox flags, timeout cleanup, Ollama tool messages, vector ranking,
verified-memory gates, embedding contracts, capability denial, model routing,
Node profiles, metrics, learning-export filtering, durable approval pause/resume,
at-most-once MCP execution, event cursors, nested traces, workflow dispatch, and
adaptive model selection.

Tests use a deterministic fake model and substitute the Docker invocation.
An actual Ollama model, PostgreSQL/pgvector service, and actual Docker test run
must still be verified on your machine.

## Remaining work

v0.5 does not yet include code-chunk embeddings, call-graph analysis, immediate
subprocess cancellation, automatic conflict resolution, browser tools,
cloud-model adapters, automatic fine-tuning, GitHub pull requests, or a
VS Code/desktop interface. The SQLite control plane is intended for a single
trusted machine, not a multi-host or multi-tenant deployment.

Generated task and orchestration worktrees are retained as audit artifacts and
can consume disk space. Automated retention and reviewed cleanup are not
implemented yet.

## Source map

| File | Responsibility |
| --- | --- |
| brain/service.py | Task lifecycle, role handoffs, cancellation, retry, and acceptance |
| brain/orchestration.py | Dependency graphs and event-safe orchestration transitions |
| brain/approvals.py | Persistent side-effect approval state machine |
| brain/telemetry.py | Durable events, cursors, and distributed trace spans |
| brain/durable_queue.py | Atomic job claims, leases, and heartbeats |
| brain/memory.py | Ollama embeddings and SQLite/PostgreSQL vector memory |
| brain/capabilities.py | Typed deny-by-default model capabilities |
| brain/routing.py | Verified-performance adaptive implementer selection |
| brain/evaluation.py | Evidence metrics and verified learning export |
| brain/workspaces.py | Detached worktrees, commits, cherry-pick integration, cleanup |
| brain/intelligence.py | Tree-sitter symbols, imports, and relevant structural context |
| brain/model.py | Ollama coordinator, implementer tool loop, and reviewer |
| brain/repository.py | Allowed source paths, snapshots, read/search tools |
| brain/sandbox.py | Isolated Python and Node profile runner |
| brain/store.py | Persistent tasks, indexes, and accepted memory |
| brain/api.py | Authenticated API and event stream |
| client.py | PowerShell-friendly command client |
| tests/test_brain.py | Runtime, Git, index, memory, API, and security checks |
| tests/test_phase5.py | Approval, event, trace, workflow, and adaptive-routing proofs |

## Primary references

- Tree-sitter Python API: https://tree-sitter.github.io/py-tree-sitter/
- Git worktrees: https://git-scm.com/docs/git-worktree
- Ollama chat and tools: https://docs.ollama.com/api/chat
- Docker network isolation: https://docs.docker.com/engine/network/drivers/none/
- Docker resource constraints: https://docs.docker.com/engine/containers/resource_constraints/
- FastAPI streaming responses: https://fastapi.tiangolo.com/advanced/custom-response/
- Ollama embeddings: https://docs.ollama.com/api/embed
- pgvector: https://github.com/pgvector/pgvector
