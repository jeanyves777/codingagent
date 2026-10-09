# Round Two — frozen evaluation configuration

Frozen at the commit that adds this file. No architectural changes during Round Two; the
checkpoint fingerprint refuses to resume if the code, settings, model or task set change.

| Item | Value |
| --- | --- |
| Tasks | 13: the 10 pilot tasks plus 3 multi-file tasks (`multifile-inventory-reservations`, `dependency-toml-migration`, `integration-pipeline-recovery`) |
| Conditions | A_free_alone, B_coding_brain, C_three_phase, claude_code |
| Repetitions | 3 independent runs per task and condition (`--repeat 3`) |
| Free model | qwen3-4b (Qwen3 4B Instruct, Q4_K_M) via Ollama 0.40.1, CPU, `BRAIN_MAX_OUTPUT_TOKENS=2048` |
| Supervisor (C) | Claude Code CLI with the signed-in subscription, `BRAIN_SUPERVISORS=claude`, default budgets (1 plan, 1 diagnosis per task, escalation after 2 failures) |
| Requirement checks | on for B and C (`BRAIN_REQUIREMENT_CHECKS` default), off for A |
| Web | `BRAIN_WEB_ALLOWLIST=pypi.org,raw.githubusercontent.com` |
| Scoring | hidden tests are authoritative; agent completion (verified, unverified, inconclusive, unchecked) and orchestration success are reported separately |

Command:

```
python -m brain.gauntlet run --repeat 3 \
  --condition A_free_alone --condition B_coding_brain --condition C_three_phase --condition claude_code \
  --output <results>.json
```
