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
  - `bad_request`
  - `not_found`
  - `refused`: a safety rule or the task state said no
  - `engine_error`
- **Events:** `{"event": {...journal event...}, "op_id": 7}`. While a long operation runs
  (`tasks.start`, `tasks.approve`, `tasks.accept` or `tasks.resume`), the server streams that
  project's activity-journal events as they happen. These are the same events
  `codingbrain watch` shows. Each event has its stage, agent, model, status, duration and summary.
  Events are what really happened; nothing is estimated or invented. The first line the server
  writes is a `ready` event that carries the API version.
- **Concurrency:** requests run concurrently, so `tasks.stop` works while `tasks.approve` is
  testing.

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
| `tasks.stop {project_id, task_id}` | Requests cancellation. |
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
