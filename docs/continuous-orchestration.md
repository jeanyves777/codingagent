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
codingbrain backlog accept       # accept the combined result: one new branch, your branch unchanged
codingbrain backlog cancel <id>
```

## What `backlog run` does

Every few seconds the conductor:

1. **Releases dependencies.** An item waits until each item it depends on is *done*, which means
   you accepted it (`backlog accept`). New work then starts from the accepted result, so a
   dependent sees the code it depends on.
2. **Checks every running attempt.** If an agent finished, the result is verified (see below).
   If the agent has shown no progress for `--stall-minutes` (default 15), the task is stopped
   and retried. If the task disappeared, the attempt is retried.
3. **Combines verified work** as soon as it is verified, while other agents keep working (see
   *Integration* below).
4. **Dispatches.** Free agent slots get the next ready item: highest priority first, then oldest.
   Slots are worked out on every tick: one local agent per 6 GB of memory and two cores, never
   more than `--max-agents` (default 4), and never more than the model server generates for at
   once (see *How many agents* below). If the machine cannot be read, it runs one.

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
- Nothing is ever accepted for you. Verified, combined work waits for `codingbrain backlog accept`.
- `backlog run` stops by itself when everything left waits on a person, and says so.
- Only one `backlog run` works on a project at a time. A lock from a process that has ended is
  ignored.

## When an item counts as verified

The agent is given the acceptance tests as its specification, in the goal. They are not placed
in its workspace, and its own copies never count. The agent's own report is never trusted. When a task finishes, the conductor copies the task's
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

Because the frozen acceptance tests are both the specification and the verifier, an attempt at
an item that has them does not ask the model to write requirement checks of its own (a plain
`codingbrain` task does, when requirement checks are on). In the backlog benchmark that
generation took 37% of all local model time and decided nothing for the item. The task's own
completion status is then "unchecked"; whether the item is verified is decided only as above.

These checks stop an agent from weakening the tests. They do not make the test process a
security boundary: code under test runs in the same process as the tests, in the sandbox.

## Integration: verified items, tested together

Each item is verified on its own worktree, which says nothing about the items together. So as
soon as items are verified, the conductor builds one combined candidate on a private worktree,
starting from the work you last accepted (or your project's HEAD). Items verified later are added
on top of the current candidate. Other agents keep working meanwhile.

- the verified items are applied one at a time (a cherry-pick of each task's change);
- after each one, every included item's frozen acceptance tests and the project's own tests run
  together in the sandbox, with the same safeguards as verification;
- an item whose change conflicts with the others, or that breaks them, is left out. It goes back
  to an agent with the conflict or failure as feedback, to be redone on top of the candidate,
  and is combined again once verified. This counts as one of its attempts.

The candidate never touches your branches or checkout. Its commit is pinned under
`refs/codingbrain/backlog/` so it is not lost. `backlog status` (and the desktop) show it.
`codingbrain backlog accept` accepts every included task through the normal acceptance (memory,
evidence) and makes one new branch at the candidate. Your current branch is unchanged, and you
merge when ready. The ledger records each item's integration verdict and the acceptance.

## How many agents: the model server decides too

Ollama queues requests beyond its `OLLAMA_NUM_PARALLEL` setting. Before the first run with more
than one agent slot, `backlog run` measures it once per model and Ollama version. It sends
simultaneous requests and checks whether their tokens arrive interleaved, which means they really
generate at the same time. Agent slots never exceed what was measured. Streams count only if they generate at the same time
and together keep at least 80% of one stream's speed. Coding Brain does not change Ollama's
settings. When the server generates for one agent at a time, the run says so and names the
setting. When it generates simultaneously but is not faster in total, the run says that instead. Measure again with `backlog run --remeasure`; results older than a week are
measured again anyway.

Measured on a 4-core, 15 GB machine (CPU only, a 3B model):

| Setting | Second request's first token | Overlap | Tokens/s, all streams together |
|---|---|---|---|
| `OLLAMA_NUM_PARALLEL=1`, 2 requests | 17.8 s (queued) | 0.00 | 6.3 (one stream alone: 8.0) |
| `OLLAMA_NUM_PARALLEL=2`, 2 requests | 0.2 s | 0.98 | 8.3–8.6 (alone: 5.4–8.5) |
| `OLLAMA_NUM_PARALLEL=4`, 4 requests | 2.4 s | 0.93–0.99 | 16.3–19.8 (alone: 5.7–8.1) |

The model server's memory grew from 2.7 GB to 3.3 GB to 4.5 GB as the setting went from 1 to
2 to 4.

## State and restarts

Everything is stored in the project's data folder (`backlog.sqlite3`): items, attempts, the
ledger, integrations and the scheduler's decisions. The task and backlog databases use SQLite's
WAL mode, so `backlog status`, the desktop and the engine API read without waiting for the
agents' writes. Before WAL, a status read was measured waiting up to 11.5 s. Stopping `backlog run` (Ctrl+C) or restarting the computer
loses nothing. The next run picks up the open attempts where they are. An attempt that a restart
or shutdown interrupted is retried, and it does not count against the item's attempts.

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
- `tests/test_conductor_cli.py`: the commands, including `backlog accept`. Accepting makes one
  branch at the combined result, your branch stays where it was, and a dependent starts from the
  accepted work.
- `tests/test_conductor_brain.py` also covers integration. Two items that edit the same lines
  conflict, and the second is redone on top of the first. An item that passes alone but breaks
  the project's tests together with another is left out and redone.
- `tests/test_inference.py`: the measurement against servers that generate one at a time and
  several at once, the remembered result, and the agent slots it allows. A real-Ollama variant
  runs in CI.
- `tests/test_sqlite_concurrency.py`: readers are not blocked by writers, a cancel request
  survives a stale write, and concurrent ledger appends keep one chain.
- `scripts/windows_acceptance.ps1`: the end-to-end acceptance test for a Windows PC, run with
  `powershell -NoProfile -ExecutionPolicy Bypass -File windows_acceptance.ps1 -Model <ollama model>`.
  It runs in its own folder and Coding Brain home, so your configuration and projects are not
  touched. It checks the real model and sandbox (`doctor --full`), backlog items with frozen
  acceptance tests, a restart in the middle of a run, independent verification, agents in
  parallel, the combined result, the ledger, your acceptance (one new branch, your branch
  unchanged) and a dependent item that builds on the accepted work. It writes
  `acceptance-report.md` and `acceptance-report.json`.
- `scripts/backlog_benchmark.py`: a whole real run (model, sandbox, integration, optional
  supervisor and acceptance) with throughput, completion, simultaneous model calls, CPU/RAM and
  premium use.
- `tests/test_engine_backlog.py` and `tests/test_desktop_backlog.py`: the engine API operations
  and the desktop's read-only Backlog tab.
- `tests/test_conductor_real.py`: a real local model through Ollama and the real Docker sandbox,
  run in CI (`installer-e2e`).
  - The conductor's guarantees are a merge gate.
  - How many items the model completes is reported separately in the job summary. It is not
    hidden, and it is not a gate.

## Limits

- On a CPU, a single model call can take many minutes when several agents share the cores.
  Calls that exceed `models.timeout` (default 600 s) fail. The item is then retried, and the
  failure (for example `ReadTimeout`) is passed on as feedback. On a slow machine, raise
  `models.timeout` in `config.json` (up to 7200).
- On a CPU, simultaneous generation shares the same cores. More agents help throughput only as
  far as the measurement shows.
- Verification needs acceptance tests that you write or approve. The conductor does not invent
  them.
- `backlog accept` is a CLI command. The engine API and desktop show the combined result but
  cannot accept it.
