# Engine API (for the desktop app and other programs)

Programs drive Coding Brain through a typed, versioned API instead of parsing command output:

```text
codingbrain api --describe    # the schema: operations, parameters, results (API 1.0)
codingbrain api --stdio       # JSON lines on stdin/stdout
```

The same operations are available in-process as `brain.local.engine.Engine(...).call(op, params)`.

## Protocol

- **Request:** `{"id": 7, "op": "tasks.start", "params": {"project_id": "...", "goal": "..."}}`
- **Response:** `{"id": 7, "ok": true, "result": {...}}`, or
  `{"id": 7, "ok": false, "error": {"code": "...", "message": "..."}}`.
- **Error codes:**
  - `unknown_op`
  - `bad_params`
  - `bad_request`: the line is not JSON, not a JSON object, or its `id` is not a string, an
    integer or null. Every line gets exactly one response, so a client never waits for a request
    the engine dropped. When the `id` itself is unusable, the response's `id` is `null`.
  - `not_found`
  - `refused`: a safety rule or the task state said no
  - `engine_error`
- **Events:** `{"event": {...journal event...}, "op_id": 7}`. While a long operation runs
  (`tasks.start`, `tasks.approve` or `tasks.resume`), the server streams the activity-journal
  events of **that operation's task** (and its subtasks) as they happen. Each event carries its
  `task_id`; other tasks in the same project never appear in this feed. To follow a task you did
  not start in this session, poll `tasks.events` with `after_seq`. These are the same events
  `codingbrain watch` shows. Each event has its stage, agent, model, status, duration and summary.
  Events are what really happened; nothing is estimated or invented. The first line the server
  writes is a `ready` event that carries the API version.
- **Pipes:** the engine keeps stdin and stdout to itself. Processes it starts (git, the sandbox,
  model CLIs) get NUL as stdin and stderr as stdout, so they can neither take a request line nor
  corrupt the response stream. On Windows this also prevents a hang: a child inheriting the
  request pipe could not start until the next request arrived.
- **Concurrency:** requests run concurrently, so `tasks.stop` works while `tasks.approve` is
  testing, over the same session. `tasks.stop` answers at once (`cancellation_requested`); the
  running `tasks.approve` then returns the task with status `cancelled` once it stops at a safe
  boundary. Nothing is applied to the project.
- **Restarts:** tasks and their journals are persisted. After the engine (or the UI) is killed,
  a new engine reports a task whose process is gone with `"interrupted": true` and its last
  status; it is never reported as passed and never restarted on its own. The next action on that
  project marks it `blocked` (retry required).

## Operations

**Read operations** never change a project, start work or spend premium budget:

| Operation | Returns |
| --- | --- |
| `engine.info` | engine version, API version, supported operations, data folder |
| `engine.describe` | the schema |
| `doctor.run {full}` | checks and readiness levels (the same as `codingbrain doctor --json`) |
| `install.status` | installer checkpoints, the last readiness level, a pending restart |
| `install.plan {profile}` | each component's state and the planned action |
| `providers.list {deep}` | provider descriptors (see below) |
| `projects.list` | the project registry, with task counts |
| `conversation.send {message, project_id}` | `reply`, `intent`, and a proposed `action` with `needs` (`confirmation`, `clarification` or `choose_project` plus `options`). **Nothing is executed.** |
| `conversation.history {project_id, limit}` | recent turns, global ones plus that project's only |
| `tasks.list {project_id}`, `tasks.get {project_id, task_id}` | task summaries |
| `tasks.events {project_id, task_id, after_seq}` | journal events after a sequence number |

**Actions** do exactly what their name says, and only when called:

| Operation | Effect |
| --- | --- |
| `projects.register {path}` | Registers an existing Git project. The project is not changed. |
| `projects.create {goal, name, root, stack, git_name, git_email}` | Creates a new project: a folder, a Git baseline and a scaffold, with every `codingbrain new` safety check. It does not start building; call `tasks.start` with `new_project: true` for that. |
| `tasks.start {project_id, goal, new_project}` | Plans the goal. Returns the proposal, the files and the diff, and the **digest**. Nothing is applied. |
| `tasks.approve {project_id, task_id, digest, decision}` | `approve` reviews the proposal, applies it in the isolated worktree and tests it in the sandbox, repairing or escalating within the budgets. Only the current digest is accepted, so an approval cannot be replayed once the task moves on. `decline` cancels. |
| `tasks.accept {project_id, task_id}` | Accepts a tested result as a **new branch**. The checked-out branch is unchanged. |
| `tasks.stop {project_id, task_id}` | Requests cancellation; returns at once. A running task stops at its next safe boundary. |
| `tasks.resume {project_id, task_id}` | Continues an unfinished task. |

There is no `--yes`, and the API adds no shortcuts. Every action goes through the same authority
checks as the CLI: path safety, the offline sandbox, review, premium budgets and digest binding.

## Provider descriptors

Each provider is described by these fields:

- `id`, `name`, `role`;
- `installed`;
- `authentication`: `signed_in`, `signed_out`, `expired`, `api_key_refused` or `not_applicable`;
- `capabilities` and `enabled`;
- `readiness`: `ready`, `needs_setup`, `needs_sign_in`, `unavailable`, `disabled` or
  `not_supported`;
- `billing_type`;
- `cost_gate`: per-task budgets and the daily limit, with API-key billing refused;
- `last_error`, `supported_actions` and `adapter`.

What each provider is today:

| Provider | Status |
| --- | --- |
| Local Ollama model | the primary worker |
| Claude Code, Codex | optional supervisors, used with your subscription sign-in |
| Meta Llama | an alternative local model through Ollama; no Meta account is involved |
| Gemini, Grok | `not_supported`: no reviewed adapter exists yet |
| Muse | `not_supported`: the product has not been identified |

A provider that is `not_supported` is never selectable and never reported as connected. Being
installed does not mean a provider is ready.

## Testing the contract

- `python -m pytest tests/test_engine.py`: the contract in-process, with fakes (validation,
  conversation that never executes, digest-bound single-use approval, two simultaneous tasks with
  separate live feeds, stop during a running sandbox over one stdio session, restart recovery).
- `python scripts/engine_client_check.py`: a real client that starts `codingbrain api --stdio` as a
  separate process with the installed Python and checks the protocol from outside, including a
  killed engine restarted from its persisted state. CI runs it on Windows (Windows PowerShell 5.1
  and PowerShell 7, Python 3.11 and 3.12).
- `python scripts/engine_client_check.py --task`: also a real task through the real model and
  sandbox (CI: Ubuntu with Ollama and Docker). The contract checks are a gate; whether the model's
  fix passes its tests is reported separately (exit status 2).

Not covered yet: the desktop app itself driving the engine on a real Windows PC, and a real task
on Windows (GitHub's Windows runners have no Linux containers for the sandbox).
