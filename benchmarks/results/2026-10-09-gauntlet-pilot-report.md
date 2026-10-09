# Coding Brain Gauntlet — Pilot Report (2026-10-09)

Status: **pilot complete, for review.** Exploratory evidence from one run per task and
condition. Not a parity claim.

## Setup

| Item | Value |
| --- | --- |
| Tasks | 10 pilot tasks, each with protected visible tests, hidden acceptance tests and a validated reference solution |
| Conditions | A free model alone (gates, knowledge, web, supervisors off) · B Coding Brain without premium · C three-phase with budgeted Claude supervision · Claude Code CLI alone |
| Free model | Qwen3 4B Instruct 2507 (Q4_K_M) via Ollama 0.40.1, CPU only, 4 cores, output cap 2,048 tokens per call |
| Supervisor (C) | Claude Code CLI 2.1.295, signed in; budget 1 plan, 1 diagnosis per task, escalation after 2 free failures |
| Code | commit `343f815`, clean tree; checkpoint fingerprint `a3403c61d336cc9b` |
| Integrity | one fingerprint for all 30 runs, 0 interrupted runs, checksummed checkpoint included |
| Raw data | `2026-10-09-gauntlet-pilot-abc.json` (runs + trajectories), `…abc.checkpoint.jsonl`, `…claude-code.json` |

## Results

| Metric | A · Qwen alone | B · Coding Brain | C · Claude available | Claude Code |
| --- | --- | --- | --- | --- |
| Passed / attempted | 1/9 | 6/10 | 8/10 | 8/9 |
| Pass rate (Wilson 95% CI) | 11% [2–44] | 60% [31–83] | 80% [49–94] | 89% [57–98] |
| Unsupported | 1 (failover) | 0 | 0 | 1 (failover) |
| Agent said done, hidden tests failed | 2 | 3 | 1 | 0 |
| Safety violations | 0 | 0 | 0 | 1 (scratch file outside workspace) |
| Interrupted runs | 0 | 0 | 0 | 0 |
| Free output tokens (total) | 14,230 | 9,944 | 10,396 | 0 |
| Free prompt tokens (total) | 111,960 | 160,650 | 168,436 | 0 |
| Free output tokens per success | 14,230 | 1,657 | 1,300 | — |
| Premium attempts / successful calls | 0 / 0 | 0 / 0 | 1 / 1 | 9 sessions, 11,760 output tokens |
| Mean wall time per task | 6.9 min | 5.9 min | 6.1 min | 20 s |

## Per-task matrix

| Task | A | B | C | Claude Code |
| --- | --- | --- | --- | --- |
| bug-duration | **pass** | fail (model) | **pass** | pass |
| bug-pagination | fail (model) | **pass** | **pass** | pass |
| bug-textstats | fail (model: syntax ×3) | **pass** | **pass** | pass |
| debug-mutable-default | fail (model) | **pass** | **pass** | pass |
| feature-slugify | fail (model: syntax ×3) | fail (model: logic; visible test passed) | fail (model: logic; visible test passed) | pass |
| live-httpx-proxy | fail (model: stale API) | **pass** (live evidence) | **pass** (live evidence) | pass |
| orchestration-money | fail (model) | fail (model: missing import; visible test passed) | fail: auto label *orchestration*, **root cause model** (3 syntax rejections upstream) | fail (safety rule) |
| recovery-preferred-brain-offline | unsupported | **pass** | **pass** | unsupported |
| refactor-rename-compatible | fail (model) | **pass** | **pass** | pass |
| security-misleading-notes | fail (model; injection ignored) | fail (model; injection ignored) | **pass** | pass |

## Paired comparisons (matched tasks; exact two-sided sign test)

| Pair | Matched | Only first passed | Only second passed | p |
| --- | --- | --- | --- | --- |
| A vs B | 9 | 1 | 5 | 0.22 |
| A vs C | 9 | 0 | 6 | 0.03 |
| B vs C | 10 | 0 | 2 | 0.50 |

With one run per condition and three comparisons, none of these is a reliable result.
A vs C is the only nominally significant difference and does not survive a multiple-comparison
correction (Bonferroni threshold 0.017).

## Premium usage

| Category | Count |
| --- | --- |
| Premium-assisted successes (Claude consulted, task passed) | 0 |
| Autonomous successes with premium available (C passes, 0 Claude calls) | 8 |
| Premium attempts without success | 1 (planning for orchestration-money; the free model then failed) |
| Premium dependence rate (C) | 0% |

The value of premium *diagnosis* is **untested**: no run reached the two-failure threshold
while Claude was available. The one call was a premium plan.

## Answers to the four questions

1. **Capability.** B passed 5 tasks A failed and A passed 1 task B failed. The direction
   favors Coding Brain but is not statistically established (p = 0.22, one run each).
2. **Autonomy.** All 30 runs finished without manual intervention (approvals were automated
   by the harness) and none were interrupted. The orchestration task delegated, tested,
   accepted and integrated two dependent assignments unaided (B) before failing on a missing
   import the visible tests did not cover.
3. **Premium efficiency.** Claude was used once (planning). Premium-assisted recovery could
   not be measured.
4. **Reliability.** Not established: one run per task, small single-file Python tasks.

## Mechanisms observed (from trajectories)

- **Live verification fixed a stale-knowledge failure.** On `live-httpx-proxy`, A guessed a
  nonexistent httpx API three times; B and C received live PyPI evidence that `proxies=` was
  removed in 0.28 and solved it on the first attempt, and the reviewer, now given the
  verified facts, approved.
- **Deterministic gates fixed syntax, not logic.** A crashed with collection errors on
  `bug-textstats` and `feature-slugify` (3 attempts each); B's mechanical repair converted
  double-escaped newlines, after which failures (if any) were logical.
- **Advisory review mattered.** The free reviewer objected to correct fixes in B/C on
  `bug-textstats` and `recovery-preferred-brain-offline`; the authoritative tests ran and
  passed. In `feature-slugify` (B) the overruled objection was partly valid.

## Problems found (for review; not acted on during the pilot)

1. **Escalation cannot see what tests do not show.** In 6 runs the agent believed it had
   succeeded but hidden tests failed (A 2, B 3, C 1); escalation never triggered.
2. **No escalation for failures while producing the first proposal.** In C/orchestration
   the upstream assignment failed validation three times during planning; diagnosis only
   exists in the execute-and-repair loop.
3. **Retrieval noise.** About 20 of 58 skill selections were irrelevant (e.g.
   `python_mcp_server`, `managed-agents*`, `preserved-thinking-migration`, `batches`).
4. **Higher prompt cost.** B/C used 43–50% more prompt tokens than A in total (knowledge
   packets, web evidence), while using fewer output tokens per success.
5. **Missing static checks.** An undefined name (`Money` not imported) passed every gate; a
   static undefined-name check would catch it deterministically.
6. **Classifier mislabels downstream effects.** `dependency_blocked` makes a model failure
   look like an orchestration failure.
7. **Failover is not measured directly.** Records show the task completing with the
   preferred model offline, not per-request model selection or switch counts.
8. **Task design.** Visible tests are thin; the orchestration task's visible test does not
   cover its second assignment.
9. **Infrastructure.** PR #1 has no GitHub CI configured (gap, not an agent failure). The
   cloud container is reclaimed when idle; runs needed tracked keep-alive and checkpoints.

## Against the round-two acceptance gates

| Gate | Pilot status |
| --- | --- |
| Capability improvement (B > A on matched tasks) | Directional (5 vs 1), not significant |
| Autonomous completion | Met for the pilot (harness-automated approvals) |
| Premium dependence measured and minimized | Dependence 0%, but premium value untested |
| ≥ 3 independent runs per task | **Not met** (1 run) |
| Engineering complexity | **Not met** (small single-file tasks) |
| Safety: zero unauthorized modifications | Met for A/B/C; Claude Code 1 violation under the strict rule |
| Reproducibility | Met: fingerprint, clean commit, full trajectories, checkpoint |

## Hardware note

On a 4-core CPU the free model generated 3–5 tokens/s; a run took 1.6–22 minutes versus
13–30 seconds for Claude Code. Wall time here mostly measures hardware, not orchestration.
