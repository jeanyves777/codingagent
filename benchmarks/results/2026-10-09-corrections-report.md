# Post-pilot corrections — results for review (2026-10-09)

Status: **six corrections implemented and verified; Round Two not started.**

## Verification

| Check | Result |
| --- | --- |
| Full test suite | 137 passed (117 before the corrections) |
| Targeted regression tests | `tests/test_corrections.py`, 20 tests |
| Each correction is load-bearing | `python benchmarks/verify_corrections.py`: with each correction disabled in a copy, its tests fail; with it enabled, they pass (6/6) |
| Hidden-test leakage | Requirement-check prompts contain only the goal (checked for all 10 tasks); checks run in a throwaway copy and never reach the evaluated workspace |
| Safety boundaries | Unchanged: offline read-only sandbox, no model-chosen commands, digest approval, manual merges |
| Benchmark tasks, advisory review | Unchanged |

## The six corrections

| # | Correction | Regression evidence |
| --- | --- | --- |
| 1 | Requirement checks written from the goal before implementation, run after visible tests pass; failures drive repairs; unmet checks never make the task worse (visible-pass version restored, `completion_verified: false`) | false success caught and repaired; restore after exhausted budget; broken checks discarded; shape validation; off by default in the library, on via factory |
| 2 | Premium diagnosis after repeated rejected initial proposals, within budget | diagnosis called once with `stage: initial_proposal`; ledger entry; empty budget respected |
| 3 | Skills matched on metadata with intent overlap, one per intent; references out of the packet | unrelated skills whose bodies match the goal are excluded |
| 4 | pyflakes undefined names; names imported from repository modules must exist | undefined `Money` rejected; cross-file missing import rejected; same-proposal definitions accepted |
| 5 | Upstream model failure is the root cause; orchestration and engineering success scored separately | re-scoring the pilot changes exactly one record: C/orchestration-money, orchestration → **model** (downstream: `dependency_blocked`) |
| 6 | Per-task `inference_log`: serving model, tokens, routes, failovers, escalations | offline preferred brain → 2 fallbacks recorded, the offline brain served 0 calls; server-side fallback model recorded; each supervisor attempt and the CLI's reported model recorded |

Re-scored pilot (separate scores):

| | A | B | C |
| --- | --- | --- | --- |
| Engineering success (hidden tests) | 1/9 | 6/10 | 8/10 |
| Orchestration success (orchestration task) | n/a | 1/1 | 0/1 (root cause: model) |
| Failure causes | model 8 | model 4 | model 2 (was 1 model + 1 orchestration) |

## Live smoke check (not Round Two)

One run each, condition B, Qwen3 4B on CPU, commit `f2a0ef6`, on the two pilot cases these
corrections target. Raw data: `2026-10-09-corrections-smoke-b.json`.

| Task | Pilot B | After corrections | What happened |
| --- | --- | --- | --- |
| orchestration-money | fail (undefined `Money`) | **pass** (hidden 3/3) | Static check rejected `undefined name 'Money'` in `invoice.py`; the model fixed the import before testing |
| feature-slugify | fail, reported as done | fail, **reported as unverified** | The model's checks were correct and caught the bug (`hello---world`); the model could not fix it in 3 attempts; the visible-pass version was kept and flagged `completion_unverified` instead of a silent false success |

Model accounting worked live: every call attributed to `qwen3-4b` by role (34 and 23
calls), 0 fallbacks, 0 escalations (condition B).

### Problems the smoke check exposed

1. **Generated checks can be wrong.** On orchestration-money, both assignments' checks
   failed on their own defects (`pytest` used without import; an invented `Money(amount=)`
   API), so correct work was flagged unverified and repairs were spent on it. Fixed
   deterministically after the smoke run: a missing `import pytest` is added, and checks with
   undefined names are rejected (tests added). Checks that assume a wrong API remain possible.
2. **Cost.** Requirement checks plus their repairs took these runs to 37–40 minutes versus a
   pilot B mean of 5.9 minutes on this CPU. Most of it was repairs driven by failing checks.
3. **Agent status.** A task with `completion_verified: false` still has status `passed`, so
   the Gauntlet's "agent said done" counts it. Records now carry `completion_verified` so
   reports can separate verified, unverified and unchecked completions.

## Decisions for review

- Whether to keep requirement checks on by default given the cost and false alarms, limit
  them to one repair attempt, or run them only to label completion (no repairs).
- Whether to re-run this smoke check with the post-smoke fix before Round Two.
