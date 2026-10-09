# Coding Brain v0.8 — a personal coding agent in three phases

Coding Brain is a local, single-user coding-agent backend. Free/local models do the
routine work; your Claude and ChatGPT subscriptions supervise strategically, only
when needed and within limits you set. Coding Brain owns orchestration, repository
context, accepted memory, isolated execution, review, testing, and Git integration.
This is an agent-runtime foundation, not a trained model or a multi-tenant service.

    PHASE 1  INTELLIGENCE    Claude Code + OpenAI Codex (your subscriptions)
                             architect, planner, reviewer, exception solver
    PHASE 2  ORCHESTRATION   Coding Brain
                             delegation, memory, goal tracking, recovery, routing,
                             supervision budgets, Engineering Knowledge Router,
                             deterministic validation, Live Web Intelligence
                             (verification broker, browser, API intelligence)
    PHASE 3  EXECUTION       free/local models (Ollama, LM Studio, llama.cpp, ...)
                             implementation, debugging, testing, refactoring, Git
                             GitHub PR + CI review as verification and fallback

| Situation | Who acts |
| --- | --- |
| Normal coding, fixes, refactoring | Free/local implementer |
| Routine review | Free reviewer, then isolated tests |
| Complex goal or multi-part orchestration | Supervisor writes the plan; free models implement |
| Repeated failures (default: 2) | Supervisor diagnoses; free worker applies the repair plan |
| Supervisor budget used up | Task fails; you may grant one more consultation (`escalate`) |
| Free models unreachable | Task pauses (`awaiting_implementer`); never escalates |
| Final integration | Draft GitHub PR from verified work, with your approval |
| PR checks fail or reviewers ask for changes | Follow-up task for the free worker updates the same PR |

## Post-pilot corrections (v0.8.1)

After Round One of the Gauntlet pilot, six targeted corrections were made: goal-derived
requirement checks to catch false successes (never committed, never from hidden tests),
premium diagnosis after repeated proposal failures, tighter skill retrieval, static
undefined-name and missing-import checks, root-cause failure classification with separate
orchestration and engineering scores, and a per-task log of the model that served each
inference, fallback and escalation. See `CHANGELOG.md`, `tests/test_corrections.py` and
`benchmarks/verify_corrections.py`.

## What v0.8 adds

v0.8 makes Coding Brain do more of the analysis, code discovery, knowledge selection
and validation itself, so the free model solves a smaller problem.

- **Deterministic validation before any model review (v0.8.1).** Proposed Python is
  compiled in memory (never run), JSON and TOML are parsed, and JavaScript/TypeScript
  are parsed with Tree-sitter. No-op proposals are rejected. Double-escaped line
  breaks, the most common small-model defect, are repaired mechanically when the
  result compiles. Failures return a short structured diagnostic with the offending
  lines, and the free model gets a bounded number of focused corrections (default 2)
  before the reviewer or the sandbox see the proposal. Test failures are classified
  (syntax, collection, test failure, timeout, infrastructure) and compacted.
- **Engineering Knowledge Router (v0.8.2).** A local library of pinned,
  license-checked engineering references and Agent Skills (`SKILL.md`), indexed with
  SQLite full-text search. For each proposal it builds an Engineering Task Packet:
  the goal, explicit success criteria, ranked code symbols and call graph, small
  relevant files inline, a few intent-selected skills, and verified fixes, within a
  character budget. Read-only `find_symbol`, `find_references`, `search_knowledge`
  and `read_skill` tools are available for that proposal.
- **Tool-first code intelligence (v0.8.3).** Optional ast-grep `structural_search`;
  the MCP gateway exposes only tools relevant to the goal; Serena can be added as a
  read-only MCP server.
- **Live web and API verification.** A policy-enforcing Web Verification Broker lets
  Coding Brain check current facts instead of trusting model memory: URLs and
  redirects, OpenAPI descriptions and their changes, documented endpoints versus the
  ones the code calls, JSON responses and data integrity, declared versus published
  package versions, and known outdated SDK usage. Results become verified evidence
  in the task packet; failures are checked upstream before any supervisor call.
- **Measurement (v0.8.4).** `benchmark --compare` replays identical tasks with the
  knowledge layer off and on and reports verified success, time, tokens, model calls,
  test runs, repairs, validation failures and supervisor calls. Every task records
  the same metrics.

## Added in v0.7

- Subscription connectors (`claude_cli`, `codex_cli`) that run the official,
  signed-in Claude Code and Codex CLIs read-only. They never read session
  credentials, strip API-key variables from the child process, and refuse to run
  when the CLI is signed in with an API key (separately billed) unless you allow it.
- A supervision engine: premium planning for complex goals, diagnosis after
  repeated failures turned into precise instructions for the free worker,
  optional takeover, per-task and daily limits, a call ledger, and human-granted
  escalation.
- GitHub verification and fallback: publish verified work as a draft PR, read CI
  checks and review comments, and create follow-up tasks that update the PR.
- Fixes found by running a real local model: structured-output finalization for
  small Ollama models, longer local timeouts, and API retry/cancel scheduling.

## Added in v0.6

- Multiple free "brains": Ollama stays the default, and any OpenAI-compatible
  server (LM Studio, llama.cpp, vLLM, LocalAI, Jan, or a free hosted tier) can be
  added. Each role (implementer, fast, reviewer, coordinator) takes an ordered list
  of brains and fails over to the next one when a brain is down or misbehaves.
- An optional, paid Claude provider (`pip install -e ".[claude]"`).
- Immediate cancellation of running sandbox tests: the container is removed as
  soon as cancellation is requested instead of running to completion.
- Call-graph extraction (callers and callees) for Python, JavaScript, TypeScript,
  and TSX, supplied to the implementing model with the structural context.
- Reviewed workspace retention: cleanup of finished tasks and orchestrations,
  age-based pruning, and private Git refs that keep accepted commits reachable.

## Retained from v0.5

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
- Ollama with one or more installed tool-capable models (free, local, the default),
  and optionally other free OpenAI-compatible servers. Claude is an optional extra.
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

On Linux or macOS the equivalent is:

    python3 -m venv .venv
    .venv/bin/python -m pip install -e ".[dev]"
    mkdir -p repositories
    export BRAIN_API_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
    export BRAIN_MODEL=YOUR_INSTALLED_IMPLEMENTER_MODEL
    export BRAIN_DURABLE_QUEUE=true
    .venv/bin/python -m uvicorn brain.api:create_app --factory --host 127.0.0.1 --port 8000
    .venv/bin/python -m brain.worker

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

## Model brains

Everything runs on free, local models by default. With only `BRAIN_MODEL` set, every
role uses that Ollama model at `BRAIN_MODEL_URL` (default http://localhost:11434).

### One free OpenAI-compatible server

    $env:BRAIN_PROVIDER = "openai"
    $env:BRAIN_MODEL_URL = "http://localhost:1234/v1"   # LM Studio; llama.cpp server uses :8080/v1
    $env:BRAIN_MODEL = "LOADED_MODEL_ID"

For a hosted free tier, also set `BRAIN_MODEL_API_KEY_ENV` to the *name* of the
environment variable that holds the key (for example `GROQ_API_KEY`).

### Several brains with failover

Copy `brains.example.json` to `brains.json`, edit the models to ones you have, and:

    $env:BRAIN_BRAINS_CONFIG = "brains.json"

Each entry under `brains` is a named backend with a `provider` (`ollama`,
`openai`, or `anthropic`), a `model`, a `url`, and, for hosted services, an
`api_key_env` naming the variable that holds its key. Keys never go in the file.
`roles` lists brains in order for `implementer`, `fast`, `reviewer`, and
`coordinator`. A role that is omitted uses the implementer list; with no roles at
all, every role uses every brain in file order.

When a brain fails (connection refused, HTTP error, timeout, malformed JSON, or an
exhausted round budget) the request moves to the next brain in the role's list,
and the task blocks only when every brain has failed. A pending tool approval is
never treated as a failure, so a side-effecting request is not re-issued
elsewhere. `client.py routes` reports which brain served each request and how
many failovers happened; `/health` shows each role's brains. Using a different
model for `reviewer` than for `implementer` gives a more independent review.

URLs must use HTTPS, or plain HTTP to this machine or a private-network address
(192.168.x, 10.x, *.local). An API key is only sent over HTTPS or to this machine.
Free hosted tiers change their model lists and limits often; check the provider
before relying on one, and remember that repository source is sent to every
configured brain.

### Optional: Claude API

This is the separately billed Anthropic API, not your Claude subscription; for the
subscription use the supervisors below. Install the extra and select it for every
role, or add an `"anthropic"` brain to `brains.json`:

    .\.venv\Scripts\python.exe -m pip install -e ".[claude]"
    $env:BRAIN_PROVIDER = "anthropic"
    $env:ANTHROPIC_API_KEY = "YOUR_KEY"

## Premium supervisors (your Claude and ChatGPT subscriptions)

Sign in to each official CLI once, in PowerShell:

    claude          # then /login and choose your Claude Pro/Max account
    codex login     # choose Sign in with ChatGPT

Then enable them, in preference order:

    $env:BRAIN_SUPERVISORS = "claude,codex"

or add `supervisors` and `supervision` sections to `brains.json` (see
`brains.example.json`). A supervisor entry takes `provider` (`claude_cli` or
`codex_cli`), and optionally `model`, `command` (path to the executable),
`timeout` in seconds, and `allow_api_billing`.

How a consultation runs: Coding Brain starts the CLI in the task's worktree with
read-only tools only (Claude Code: `Read,Grep,Glob` in `dontAsk` mode; Codex:
`exec --sandbox read-only --ephemeral`), passes a concise, untrusted-data prompt
on standard input, and requires a JSON answer that matches a fixed schema. Before
the first call it checks `claude auth status` or `codex login status` and refuses
API-key sign-ins. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and related variables are
removed from the CLI's environment so usage stays on your subscription. Any
supervisor that fails (signed out, error, timeout) is skipped for the next one.

`supervision` settings (defaults shown):

| Setting | Default | Meaning |
| --- | --- | --- |
| order | file order | Supervisors to try, first to last |
| escalate_after | 2 | Free failures (test, review, or invalid output) before a diagnosis |
| diagnose_budget | 1 | Diagnoses per task |
| plan_budget | 1 | Premium plans per task |
| decompose_budget | 1 | Premium orchestration graphs per orchestration |
| review_budget | 0 | Premium final reviews per task (with final_review) |
| daily_limit | 20 | Supervisor calls across all tasks in 24 hours |
| plan_complex_tasks | true | Premium plan when the goal scores as complex |
| plan_orchestrations | true | Premium dependency graph for `delegate` |
| final_review | false | Premium review after tests pass |
| takeover | false | Let a diagnosis supply files directly for one attempt |

Request a premium plan for one task with `client.py submit --premium-plan`.
When a task fails with its budget used, it records `supervisor_budget_exhausted`.
Grant exactly one more diagnosis, after which the task returns to `proposed` for
your approval:

    .\.venv\Scripts\python.exe client.py escalate --id TASK_ID
    .\.venv\Scripts\python.exe client.py supervision --id TASK_ID

`supervision` lists the configured supervisors, the task's remaining budget, and
the ledger of calls. Only successful consultations count toward a task budget;
every attempt counts toward the daily limit. Accepted tasks record which
supervisors helped in long-term memory.

If no free implementer is reachable, the task pauses as `awaiting_implementer`
instead of escalating. Retry it when a model is running.

## GitHub pull requests and CI

Publish accepted work (or a completed orchestration) as a draft pull request:

    $env:GITHUB_TOKEN = "TOKEN_WITH_PULL_REQUEST_WRITE"
    .\.venv\Scripts\python.exe client.py publish --id TASK_ID
    .\.venv\Scripts\python.exe client.py pr-feedback --id TASK_ID
    .\.venv\Scripts\python.exe client.py follow-up --id TASK_ID

`publish` pushes the verified commit to `coding-brain/<id>` on the repository's
`origin` with your normal Git credentials and opens a draft PR against the current
branch (`--remote`, `--base`, and `--github-repository owner/name` override this).
`pr-feedback` reads check runs, commit statuses, reviews, and comments for the PR
head. `follow-up` creates a free-worker task from failing checks and review
comments, starting from the PR head; publishing that task updates the same PR.
GitHub verifies and reports; it does not write code. Use `BRAIN_GITHUB_TOKEN_ENV`
to read the token from another variable.

## Engineering knowledge

The router is on by default (`BRAIN_KNOWLEDGE=false` disables it). Without imported
knowledge it still sends the compact packet with code context, success criteria and
verified fixes. To add engineering skills and references, import them on the host;
network access happens here, never inside the test sandbox:

    .\.venv\Scripts\python.exe -m brain.knowledge sync knowledge.example.json
    .\.venv\Scripts\python.exe -m brain.knowledge list
    .\.venv\Scripts\python.exe -m brain.knowledge search "failing test root cause"

`knowledge.example.json` pins Superpowers (MIT, `skills/`), Anthropic's Agent Skills
(Apache-2.0 skills only), and three sections of Microsoft's Engineering Playbook
(CC-BY-4.0). Each source is a Git URL pinned to a commit or a local directory. Only
Markdown is read: scripts and other files are counted and ignored, never executed,
and Git hooks are disabled during checkout. The nearest license file decides each
document's license; proprietary or unlicensed documents are skipped and listed, and
a declared license must match the detected one. Imported knowledge lives in
`brain-data/knowledge.sqlite3`, separate from accepted task memory, and is presented
to models as untrusted guidance.

| Variable | Default | Meaning |
| --- | --- | --- |
| BRAIN_KNOWLEDGE | true | Enable the Engineering Knowledge Router |
| BRAIN_KNOWLEDGE_BUDGET | 6000 | Packet budget in characters, excluding inline sources |
| BRAIN_KNOWLEDGE_SKILLS | 3 | Maximum skills and references per packet |
| BRAIN_VALIDATION_RETRIES | 2 | Focused corrections after a deterministic validation failure |

For structural search install `pip install -e ".[tools]"` (ast-grep). To use
Serena's semantic navigation, run it as a streamable-HTTP MCP server and keep only
its read-only tools in `mcp.json` (see `mcp.example.json`); verify the tool names
with `client.py mcp-tools`.

Measure the effect on your own tasks and hardware:

    .\.venv\Scripts\python.exe -m brain.benchmark benchmarks\textstats.json --compare --no-supervisors --output compare.json

## Live web and API verification

Enable it by naming the hosts Coding Brain may contact:

    $env:BRAIN_WEB_ALLOWLIST = "pypi.org,registry.npmjs.org,raw.githubusercontent.com,docs.stripe.com"

All web access goes through the broker in the orchestrator; the Docker test sandbox
stays offline. The broker allows only `https` on the default port, only allowlisted
hosts (`*.example.com` patterns are supported), and only GET and HEAD. Every host's
DNS answers are checked on every redirect hop, and private, loopback, link-local,
multicast, reserved and cloud-metadata addresses are refused. It sends no cookies or
credentials and does not read `.netrc`. It honors `robots.txt` when reading pages
(documented JSON APIs such as package registries are called directly), paces each
host, caps responses (2 MB) and time (20 s), and caches responses with ETag
revalidation. Each result is evidence: URL, final URL, time, HTTP status, SHA-256,
redirects, and an outcome of `verified`, `failed` or `inconclusive`.

What happens automatically:

- **Preflight** (once per task): URLs in the goal are checked; OpenAPI descriptions
  are fetched, snapshotted and compared with the last snapshot; endpoints the code
  calls are checked against the description; declared packages mentioned in the
  goal are checked against PyPI or npm; known outdated SDK patterns
  (`brain/data/deprecations.json`) are confirmed against the live registry. The
  result goes into the task packet with an instruction never to substitute a
  guessed URL or endpoint for one that failed verification.
- **Import gate**: a Python proposal that imports a repository module which does not
  exist is rejected deterministically.
- **Upstream check** after a test failure and before any premium consultation:
  missing modules (local or PyPI), URLs in the failure, and outdated SDK usage.

Read-only tools for the free model: `web_fetch`, `check_url`, `package_info`,
`api_endpoints`, and with `BRAIN_BROWSER=true`, `browser_inspect`. The browser
renders JavaScript pages with Playwright and reports compact text, console errors,
failed requests and a network summary. Every request the page makes passes the
same policy. The model cannot click, type, submit forms or run scripts. Set
`BRAIN_BROWSER_EXECUTABLE` to use an installed Chromium.

| Variable | Default | Meaning |
| --- | --- | --- |
| BRAIN_WEB_ALLOWLIST | empty (off) | Hosts the broker may contact |
| BRAIN_WEB_TTL_SECONDS | 86400 | Cache freshness before revalidation |
| BRAIN_WEB_MAX_BYTES | 2000000 | Response size limit |
| BRAIN_WEB_RESPECT_ROBOTS | true | Honor robots.txt when reading pages |
| BRAIN_WEB_FIXTURES | empty | Directory of recorded responses (deterministic mode) |
| BRAIN_WEB_RECORD | false | Record missing fixtures from the network |
| BRAIN_BROWSER | false | Offer browser_inspect |

API contract testing: `brain.api_intelligence.validate_response` and `integrity`
validate recorded or staging responses against the documented schema and configured
invariants (required non-null fields, uniqueness, freshness). Coding Brain never
sends write requests or fuzzing traffic; run tools such as Schemathesis yourself
against mock servers or authorized staging environments.

## Coding Brain Gauntlet (evaluation)

The Gauntlet measures whether the architecture, not a lucky run, produces results.
Every task has hidden acceptance tests the agent never sees, a reference solution,
and optional safety rules (protected files, canary strings planted by misleading
content). `validate` proves each task is sound: the original fails the hidden tests
and the reference passes.

    .\.venv\Scripts\python.exe -m brain.gauntlet validate
    .\.venv\Scripts\python.exe -m brain.gauntlet run --condition A_free_alone --condition B_coding_brain --condition C_three_phase --repeat 3 --output gauntlet.json
    .\.venv\Scripts\python.exe -m brain.gauntlet run --condition claude_code --output baseline.json

| Condition | What runs |
| --- | --- |
| A_free_alone | The free model with deterministic gates, knowledge, web, and supervisors off |
| B_coding_brain | Free model + Knowledge Router, code tools, gates, web verification; no premium |
| C_three_phase | B + budgeted Claude/Codex supervision (`BRAIN_SUPERVISORS`) |
| claude_code / codex | The premium CLI alone in a copy of the repository |

Each run is evaluated identically: final files are copied to a clean directory, the
hidden tests are added, and they run in the offline sandbox with the task's image.
The report gives, per condition, the pass rate with a Wilson 95% interval, the
premium-dependence rate (share of successes that used Claude/Codex), premium calls
and free output tokens per success, mean time, safety violations, results by
category, and paired win/loss counts on identical tasks. Trajectories are kept for
review. A task that needs a capability a condition lacks (for example `failover`
for A, or shell access) is reported as unsupported, never counted or dropped.

`gauntlet/tasks` holds a 10-task pilot set: bug fixes, features and a compatible
refactor, a live API migration (httpx 0.28), debugging, a two-part orchestration,
prompt injection through misleading repository notes, and recovery with the
preferred free model offline. Add tasks in the same format to grow it toward the
100-task gauntlet; keep hidden tests out of `repo/`.

External suites (Terminal-Bench, SWE-bench variants, SkillsBench, MCPMark) are not
bundled. They need their own harnesses, and terminal-centric suites assume shell
access that Coding Brain's free models deliberately do not have; such tasks must be
run through an adapter and reported as unsupported where a capability is missing.
On CPU-only hardware, long tasks also measure hardware speed, so report model
capability, orchestration quality and hardware separately.

## Cancellation and recovery

    .\.venv\Scripts\python.exe client.py cancel --id TASK_OR_ORCHESTRATION_ID
    .\.venv\Scripts\python.exe client.py retry --id FAILED_TASK_ID

Cancellation is cooperative for model requests: a running model request is allowed
to reach the next safe boundary. A running Docker test is stopped immediately; the
container is removed within about half a second of the cancellation request. Restarted in-flight tasks are marked blocked
instead of being silently resumed. Retry creates a fresh workspace from the
current valid base. Orchestrations themselves cannot yet be retried; retry the
blocked child or create a new orchestration.

With the durable queue enabled, queued jobs survive API and worker restarts.
Workers renew a lease while operating. An expired lease is marked failed and its
task becomes blocked when the API reconciles the queue. This avoids automatically
replaying an operation that may already have changed a worktree.

## Workspace retention and cleanup

Task and orchestration worktrees are kept after completion as audit artifacts.
Remove them once reviewed:

    .\.venv\Scripts\python.exe client.py cleanup --id TASK_OR_ORCHESTRATION_ID
    .\.venv\Scripts\python.exe client.py prune --days 7

Only finished work (accepted, completed, failed, blocked, cancelled, or
integration_conflict) can be cleaned up. Cleaning an orchestration also cleans
its assignments and requires all of them to be finished. Before an accepted
commit's worktree is removed it is pinned at `refs/coding-brain/tasks/TASK_ID`;
an orchestration's integration head is pinned at
`refs/coding-brain/orchestrations/ORCHESTRATION_ID`. Merge from those refs later:

    git -C repositories\demo merge --ff-only refs/coding-brain/orchestrations/ORCHESTRATION_ID

Delete a ref with `git update-ref -d` once it is no longer needed. Pruning only
considers standalone tasks and orchestrations whose last event is older than the
window.

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

The structural index extracts named declarations, imports, and calls. Relevant
entries are supplied to the implementing model before it edits, together with a
`call_graph` listing the direct callers and callees of symbols named in the goal.
Calls are resolved by name only, not by type or import, so same-named functions in
different files are not distinguished. Indexing is structural, not yet semantic
embedding search.

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
| BRAIN_PROVIDER | ollama; or openai (any OpenAI-compatible server) or anthropic |
| BRAIN_MODEL | Required for ollama and openai; claude-opus-5-5 for anthropic |
| BRAIN_BRAINS_CONFIG | Optional path to a multi-brain JSON file; overrides the provider variables |
| BRAIN_MODEL_API_KEY_ENV | Optional name of the variable holding an openai-provider key |
| BRAIN_EMBEDDING_URL | Ollama URL for embeddings; defaults to BRAIN_MODEL_URL for ollama |
| BRAIN_SUPERVISORS | Optional; claude and/or codex, using the signed-in CLIs |
| BRAIN_MAX_FREE_ATTEMPTS | 3; free attempts before failing without a supervisor |
| BRAIN_REQUIREMENT_CHECKS | true; goal-derived requirement checks run after the visible tests pass |
| GITHUB_TOKEN | Needed for publish and pr-feedback |
| BRAIN_ANTHROPIC_EFFORT | high; low, medium, high, xhigh, or max |
| BRAIN_ANTHROPIC_FALLBACKS | true; server-side refusal fallbacks for Claude |
| BRAIN_FAST_MODEL | Defaults to BRAIN_MODEL; optional smaller implementer |
| BRAIN_COORDINATOR_MODEL | Defaults to BRAIN_MODEL |
| BRAIN_REVIEW_MODEL | Defaults to BRAIN_MODEL |
| BRAIN_MODEL_URL | http://localhost:11434; the /v1 endpoint for openai |
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
user, a 120-second timeout, and removal on cancellation. A container is not a virtual-machine security
boundary. Use only trusted repositories and prebuilt sandbox images.

Snapshots exclude hidden paths, symlinks, common credential names, dependencies,
build output, and Git metadata. Limits are 1,000 files, 200 KB per file, and
20 MB total. Credential-name filtering is not comprehensive. Repository source
is sent to the configured model endpoint.

The model cannot choose arbitrary shell commands. It can list, search, and read
allowed source files, then propose at most ten complete replacement files. Tests
execute a fixed command. Dependency installation does not occur during a task.

## Validation

The release passed 137 automated tests covering path and data restrictions,
Tree-sitter Python and TypeScript indexing, graph cycle rejection, proposal
approval, memory gating, worker limits, repository isolation, real worktree
commits, dependency inheritance, downstream blocking, integration conflicts,
cancellation, retry, restart handling, authentication, events, repository
context, sandbox flags, timeout cleanup, Ollama tool messages, vector ranking,
verified-memory gates, embedding contracts, capability denial, model routing,
Node profiles, metrics, learning-export filtering, durable approval pause/resume,
at-most-once MCP execution, event cursors, nested traces, workflow dispatch, and
adaptive model selection, the OpenAI-compatible and Claude tool loops, brain
failover and configuration validation, provider selection, subscription CLI
invocation and API-key refusal, supervision budgets and escalation, takeover,
premium planning, offline pausing, PR publishing, feedback, and follow-up,
deterministic validation and bounded correction, mechanical repair, failure
classification, knowledge import with license checks, packet budgets, task tools,
failing-tool handling, MCP tool selection, structural search, and the comparison
harness, supervised sandbox cancellation and timeout, call-graph extraction, and
commit-pinning workspace cleanup, and the six post-pilot corrections (requirement
checks, proposal-failure escalation, retrieval filtering, static checks, root-cause
classification, and model-level accounting).

Tests use a deterministic fake model and substitute the Docker invocation.
Model responses from every provider are substituted in tests. An actual model,
PostgreSQL/pgvector service, and actual Docker test run must still be verified on
your machine.

## Remaining work

v0.8 does not yet include hash-checked targeted patches (proposals still carry
complete files, which dominates small-model output tokens), automatic polling of PR
checks, merging, code-chunk embeddings, type-resolved call graphs,
cancellation of an in-flight model request, automatic conflict resolution, browser
tools, automatic fine-tuning, GitHub pull requests, or a VS Code/desktop interface.
The SQLite control plane is intended for a single trusted machine, not a
multi-host or multi-tenant deployment. Cleanup is explicit or age-based on request;
there is no background retention job.

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
| brain/workspaces.py | Detached worktrees, commits, cherry-pick integration, pinned refs |
| brain/intelligence.py | Tree-sitter symbols, imports, call graph, and relevant context |
| brain/model.py | Ollama coordinator, implementer tool loop, and reviewer |
| brain/openai_compatible.py | OpenAI-compatible coordinator, implementer tool loop, and reviewer |
| brain/anthropic_model.py | Optional Claude coordinator, implementer tool loop, and reviewer |
| brain/brains.py | Named brains, URL policy, and per-role failover chains |
| brain/factory.py | Environment configuration and provider selection |
| brain/subscriptions.py | Phase 1: Claude Code and Codex CLI supervisor connectors |
| brain/supervision.py | Phase 2: supervision budgets, daily cap, and call ledger |
| brain/supervised.py | Phase 2: premium planning, diagnosis, takeover, escalation |
| brain/github.py | Phase 3: GitHub pull request, checks, and review client |
| brain/publishing.py | Phase 3: publish, PR feedback, and follow-up tasks |
| brain/validators.py | Deterministic syntax/scope gates, mechanical repair, failure classes |
| brain/knowledge.py | Knowledge library, Engineering Knowledge Router, task tools |
| brain/skills.py | SKILL.md and Markdown parsing |
| brain/web_verification.py | Web Verification Broker: policy, cache, evidence, fixtures |
| brain/browser.py | Controlled Playwright inspection of rendered pages |
| brain/api_intelligence.py | OpenAPI discovery, diffs, endpoint and response checks, SDK versions |
| brain/web_intelligence.py | Preflight evidence, upstream checks, and web tools |
| brain/repository.py | Allowed source paths, snapshots, read/search tools |
| brain/sandbox.py | Isolated Python and Node profile runner |
| brain/store.py | Persistent tasks, indexes, and accepted memory |
| brain/api.py | Authenticated API and event stream |
| client.py | PowerShell-friendly command client |
| tests/test_brain.py | Runtime, Git, index, memory, API, and security checks |
| tests/test_phase5.py | Approval, event, trace, workflow, and adaptive-routing proofs |
| tests/test_phase6.py | Brains, failover, providers, cancellation, call graph, and cleanup proofs |
| tests/test_phase7.py | Subscription supervisors, supervision policy, and GitHub fallback proofs |
| tests/test_phase8.py | Validation, knowledge, task tools, MCP selection, and comparison proofs |
| tests/test_phase9.py | Web policy, evidence, cache, API intelligence, browser, and preflight proofs |
| brain/gauntlet.py | Paired evaluation harness with hidden tests and safety checks |
| gauntlet/tasks | Pilot evaluation tasks (repo, hidden tests, reference solution) |
| tests/test_gauntlet.py | Harness, safety, statistics, and unsupported-capability proofs |

## Primary references

- Tree-sitter Python API: https://tree-sitter.github.io/py-tree-sitter/
- Git worktrees: https://git-scm.com/docs/git-worktree
- Ollama chat and tools: https://docs.ollama.com/api/chat
- Docker network isolation: https://docs.docker.com/engine/network/drivers/none/
- Docker resource constraints: https://docs.docker.com/engine/containers/resource_constraints/
- FastAPI streaming responses: https://fastapi.tiangolo.com/advanced/custom-response/
- Ollama embeddings: https://docs.ollama.com/api/embed
- Claude Messages API and tool use: https://platform.claude.com/docs/en/api/messages
- pgvector: https://github.com/pgvector/pgvector
