# Live activity, traces and snapshots (0.12)

`codingbrain run` shows what is happening while it happens. Each line is a real event, written to
the project's journal at the moment it occurs:
- the stage, with its start time and duration;
- which agent is responsible, and with which model;
- what the models explained;
- the test results;
- every approval Coding Brain waits for.

While a model request is running, a status line shows how long it has taken and when its last
heartbeat arrived. If heartbeats stop, the line says the request may be stalled instead of going
silent.

```
[00:00] + Goal received
[00:00] > Coding Brain — Validating Git and preparing an isolated worktree
[00:00] + Project and Git validation (0.0 s)
[00:00] > Implementer — Generating the proposed changes
… Implementer (qwen2.5-coder:7b) working on proposal generation — 01:12 elapsed, last heartbeat 4 s ago
[01:15] +   implementer served by qwen2.5-coder:7b (3120 in, 410 out)
[01:15] + Implementer explanation (model-provided): Return the sum instead of the difference, and test it.
      files: calculator.py, test_calculator.py
[01:15] ? Waiting for approval to review, apply and test: calculator.py, test_calculator.py
[01:20] > Sandbox — Running the tests in the offline sandbox (attempt 1)
[01:24] + Sandbox passed (exit 0): 2 passed in 0.03s
```

Coding Brain only shows two kinds of explanation, and labels which is which:
- **model-provided:** text a model returned as part of its answer, such as a plan, a supervisor's
  diagnosis or a reviewer's reason;
- **observed:** facts Coding Brain recorded itself, such as a failure category, a retry, a route
  or the selection of a supervisor.

It never asks for, invents or shows a model's private reasoning. It never shows a percentage of
progress.

## Commands

| Command | What it shows |
| --- | --- |
| `codingbrain run "goal"` | Live activity, on by default. Add `--verbose` for heartbeats, routes and snapshots; `--quiet` for failures, approvals and outcomes only; `--plain` for no in-place status line; `--json` for one JSON event per line. |
| `codingbrain activity` | The tasks running or waiting in this project: the current stage and agent, the model request in flight with its heartbeat age, whether the process running it is still alive, and the latest events. |
| `codingbrain watch [TASK]` | Follows a task from any terminal until it finishes or needs you. Add `--history` to start from its first event. |
| `codingbrain trace [TASK]` | The full history: every stage with its timing and agent, the explanations and decisions, the tests, the models with calls and tokens ("unknown" when a provider does not report them), fallbacks, premium consultations, retries, and the snapshots. `--json` is available. |
| `codingbrain snapshots [TASK]` | The snapshots of a task. |
| `codingbrain snapshot show ID [--diff]` | A snapshot: state, changed files, plan, tests, visual evidence and how to resume. |
| `codingbrain snapshot diff A B` | What changed between two snapshots: files, with their diffs, and state. |
| `codingbrain snapshot restore ID` | Creates a new branch with the snapshot's files. It asks first; your working files and current branch are never changed. |
| `codingbrain snapshot purge [--older-than-days N \| --task ID]` | Deletes old snapshots and journal events (retention). |

`activity`, `watch`, `trace` and `snapshots` are read-only. You can run them in another terminal
while a task is running, and they never mark it as interrupted.

## Snapshots

A snapshot is taken at each boundary: initial state, after the proposal, before and after the
changes, before and after the tests, before and after each repair, after visual verification,
and before acceptance. Each snapshot records:
- the task, parent task and state;
- the Git baseline;
- the changed files, as a content-addressed manifest, and the diff;
- the plan and the requirement checks;
- the test and visual evidence, including desktop, tablet and mobile screenshots for frontend
  tasks;
- failures and retries, and model usage;
- the command to resume.

Snapshots are stored under the project's data folder, never in the project. Identical content is
stored once.

### What a snapshot stores, and what it never stores

A changed file's content is stored only if all of these hold:

- it is a regular file inside the task workspace;
- no folder on its path, and not the file itself, is a symbolic link, junction or other reparse
  point (checked on every Windows Python, including 3.11);
- its name does not look like a secret or credential (`.env`, keys, `credentials.json` and similar);
- its text holds no credential: no known key format (API keys, access tokens, private keys, JWTs),
  no password, token, secret or API key assigned a quoted literal, and in configuration files
  (`.yaml`, `.toml`, `.ini`, `.json`, `.env` and similar) none assigned any value. Ordinary code such
  as `token = request.headers.get(...)` is stored.

The checks run before the file is opened or hashed, and the opened file must be the one that was
checked. Anything else is listed in the snapshot's manifest with the reason it was excluded, and
its content is never stored. A restore leaves such a file at its baseline version and says so; it
never writes a redacted copy in its place.

The diff is built only from stored files, and it is redacted. Screenshots and other artifacts are
stored only if they are image files. They show the app preview as rendered, so anything visible on
that page is in the image. For a task run with `--sensitive`, a snapshot keeps only the changed
paths: no file content, no diff and no screenshots.

## Where the data lives

| Data | Location |
| --- | --- |
| Journal | `data\projects\<project>\telemetry.sqlite3` |
| Snapshots | `data\projects\<project>\snapshots\` |

Journal entries and snapshot metadata are redacted before they are written, and snapshot file
content follows the rules above. Both keep growing until you purge them with `snapshot purge`,
which also deletes every stored object that no remaining snapshot references.

## Heartbeats

A heartbeat is written every 10 seconds while a model request is running. Change the interval
with the `BRAIN_HEARTBEAT_SECONDS` environment variable. A status line that says "no heartbeat"
means that for more than three intervals the request has neither answered nor reported. Press
Ctrl+C to pause, and run `codingbrain resume` to continue.
