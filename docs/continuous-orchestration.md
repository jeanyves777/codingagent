# Continuous orchestration: `codingbrain backlog`

The backlog keeps local agents working through a list of goals without anyone driving each task.
It is a layer over the existing Brain, not a second orchestrator: every attempt is an ordinary
Brain task (its own git worktree, the normal proposal and repair loop, the configured supervisor
budgets), and every approval gate stays where it was.

```
codingbrain backlog add "Add slugify(text) to slug.py" --priority P0 \
    --accept-file tests/test_slug.py --accept-node tests/test_slug.py::test_slugify
codingbrain backlog add "Use slugify in the export" --depends <id-of-the-first-item> \
    --accept-file tests/test_export.py --accept-node tests/test_export.py::test_names
codingbrain backlog run          # works until everything is verified, blocked, or waits on you
codingbrain backlog status       # items, agents, attempts (add --json)
codingbrain backlog ledger       # verification evidence and whether the chain is intact
codingbrain backlog cancel <id>
```

## What `backlog run` does

Every few seconds the conductor:

1. **Releases dependencies.** An item waits until each item it depends on is *done*, which means
   you accepted it. Verified but unaccepted work is not on your branch yet, so dependents keep
   waiting for you.
2. **Checks every running attempt.** If an agent finished, the result is verified (see below).
   If the agent has shown no progress for `--stall-minutes` (default 15), the task is stopped
   and retried. If the task disappeared, the attempt is retried.
3. **Dispatches.** Free agent slots get the next ready item: highest priority first, then oldest.
   Slots are worked out on every tick from the machine: one local agent per 6 GB of memory and
   two cores, never more than `--max-agents` (default 4). If the machine cannot be read, it runs
   one.

A failed or rejected attempt goes back into the queue right away with the failure as feedback.
Retries move up a ladder: `local`, then `local_with_feedback`, then `premium_plan`, where a
configured supervisor (Claude or Codex CLI) plans the attempt. The premium step is used only when
a supervisor is configured and its daily budget is not spent; otherwise the item is blocked with
the reason. An item is also blocked when it fails the same way twice in a row or reaches
`--max-attempts` (default 3).

## Approvals stay with you

- Proposals run without asking only if `autonomy.execution` is `auto` in your configuration.
  Otherwise items stop at `awaiting_approval` until you approve them (`codingbrain resume`).
- Tool approvals inside a task work as before.
- Nothing is ever accepted for you. A verified item waits for `codingbrain accept`.
- `backlog run` stops by itself when everything left waits on a person, and says so.
- Only one `backlog run` works on a project at a time. A lock from a process that has ended is
  ignored.

## When an item counts as verified

The agent's own report is never trusted. When a task finishes, the conductor copies the task's
workspace and then:

- writes back the acceptance files exactly as they were when you added the item, so the agent
  cannot edit the tests;
- restores the project's test configuration (`conftest.py`, `pytest.ini`, `tox.ini`,
  `setup.cfg`, `pyproject.toml`, `coding-brain.json`) from the task's starting point, so the agent
  cannot skip tests or change the test command;
- refuses a change that adds a file which would replace the test runner (`pytest.py`, `.pth`
  files, `sitecustomize.py` and similar);
- runs the acceptance tests in the sandbox and requires every listed test to pass. Skipped,
  expected-to-fail or deselected tests, or no tests at all, count as failure.

Each verdict is written to a hash-chained ledger with the command, exit code and a hash of the
output. `backlog status` reports whether the chain is intact. An item without acceptance tests
can never be verified and is blocked when its task finishes.

These checks stop an agent from weakening the tests. They do not make the test process a
security boundary: code under test runs in the same process as the tests, in the sandbox.

## State and restarts

Everything is stored in the project's data folder (`backlog.sqlite3`): items, attempts, the
ledger and the scheduler's decisions. Stopping `backlog run` (Ctrl+C) or restarting the computer
loses nothing. The next run picks up the open attempts where they are.

## From the desktop and other programs

The [engine API](engine-api.md) offers the backlog as `backlog.status` and `backlog.ledger`
(reads that never start the service or a run) and `backlog.add` (the same checks as
`backlog add`; it starts nothing). The desktop's Backlog tab shows the status read-only: counts,
busy agents, items and their reasons, "waiting for you" and whether the ledger is intact. Neither
the API nor the desktop can run the backlog: that stays `codingbrain backlog run`.

## Evidence

- `tests/test_conductor.py`: the scheduler with scripted agents. Covers parallel slots, priority,
  reassignment, feedback, escalation, stalls, a lying agent, tampering, dependencies, restart,
  cancel and resources.
- `tests/test_conductor_brain.py`: the real Brain with a stand-in model. Two agents' model calls
  overlap, an agent that skips the tests or replaces pytest is caught and retried, and your
  acceptance releases a dependent.
- `tests/test_conductor_cli.py`: the commands.
- `tests/test_engine_backlog.py` and `tests/test_desktop_backlog.py`: the engine API operations
  and the desktop's read-only Backlog tab.
- `tests/test_conductor_real.py`: a real local model through Ollama and the real Docker sandbox,
  run in CI (`installer-e2e`).
  - The conductor's guarantees are a merge gate.
  - How many items the model completes is reported separately in the job summary. It is not
    hidden, and it is not a gate.

## Limits

- Local agents share one Ollama server. Unless `OLLAMA_NUM_PARALLEL` allows concurrent requests,
  Ollama queues them. Agents then work in parallel on everything except model generation.
- Verification needs acceptance tests that you write or approve. The conductor does not invent
  them.
- Work from different items is not merged or re-tested together. Each item is verified on its
  own worktree. Merging stays your decision through `accept`.
