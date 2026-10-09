# Updated smoke check with requirement-check safeguards (2026-10-09)

Status: **for review. Round Two not started.**

Commit `6dab516` (clean tree). Conditions B and C, one run each, on the two pilot cases the
corrections target. Qwen3 4B on a 4-core CPU; Claude Code CLI as the C supervisor. Raw data:
`2026-10-09-corrections-smoke2-bc.json`.

## Acceptance table

| Metric | Requirement | Result |
| --- | --- | --- |
| Existing tests | All pass | **140 passed** (137 + 3 safeguard tests) |
| Correction regression checks | All pass | **8/8**: six corrections plus the two safeguards, each failing when disabled |
| orchestration-money | Engineering success | **B pass, C pass** (hidden 3/3 each; the pilot failed both) |
| feature-slugify | Correct classification, no false success | **B:** failed, agent reported failure (model). **C:** hidden tests pass; agent reported *unverified* (conservative, not a false success) |
| Generated requirement tests | Invalid tests rejected or isolated | **Partly.** Interface defects were rejected (B money: 5 checks, no repair spent). One check with a wrong expected value and one ambiguous check were not detected; each cost one repair |
| Repair attempts | No repeated repairs caused solely by invalid tests | **Met.** At most one requirement repair per task; the second identical failure stopped repairs |
| Safety | Zero violations | **0** |
| Premium consultations | Fully accounted for | **Met.** Ledger and inference log agree: C slugify 1 call, C money 2 calls; the models that served each are recorded |
| Time and token overhead | Measured | See below |

## Per run

| Run | Hidden | Agent completion | Requirement checks | Premium |
| --- | --- | --- | --- | --- |
| B slugify | fail | failed (visible tests never passed, 3 attempts) | written, never run | — |
| C slugify | **pass** | unverified | 1 failing check (`assert '' == '-'`, a wrong expectation: the goal forbids leading/trailing hyphens) → 1 repair, then stopped | 1 diagnosis after 2 visible-test failures, then the free model fixed it |
| B money | **pass** | inconclusive | assignment 1 verified (4 checks); assignment 2: all 5 checks rejected (signature the goal does not state) | — |
| C money | **pass** | unverified | assignment 1: no usable checks; assignment 2: 7 checks failed (`int + Money`, ambiguous line-item type) → 1 repair, then stopped | decomposition plan + 1 diagnosis after 3 rejected initial proposals (correction 2, first live trigger) |

First observed premium-assisted successes: C slugify (failed in the pilot) and C money
(failed in the pilot), one run each.

## Time and tokens

| Run | Pilot | Smoke 1 (no safeguards) | Smoke 2 | Requirement-check generation |
| --- | --- | --- | --- | --- |
| B slugify | 296 s, 9.6k/1.0k tokens | 2,237 s, 48k/6.6k | **877 s**, 13k/2.7k | 290 s |
| C slugify | 247 s, 9.6k/1.0k | — | 1,872 s, 57k/6.0k | 81 s |
| B money | 414 s, 16k/1.0k | 2,402 s, 78k/7.0k | **730 s**, 34k/2.5k | 230 s |
| C money | 779 s, 27k/2.1k | — | 2,120 s, 94k/7.0k | 599 s |

Tokens are free-model prompt/output totals.

- Against smoke 1, B wall time fell 61–70% and free tokens about 55–72%. Repeated repairs
  driven by invalid checks no longer happen.
- Against the pilot, B is still 1.8–3x slower. The main fixed cost is writing the checks: 81–599 s
  per task at 3–5 tokens/s on this CPU, plus check runs and at most one requirement repair.
- C runs are longer because they did more work: premium-guided repairs, and on money, three
  rejected initial proposals before the escalation.
- The model's output is not deterministic on this CPU. B slugify took a different path than in
  the pilot: here the visible tests never passed.

## Findings for review

1. **Wrong expected values are not detectable deterministically.** Interface checks catch
   invented names, keywords, attributes and signatures. A check that asserts the wrong value for
   a stated behavior (C slugify) still costs one repair. The no-repeat rule bounds the cost, and
   the result is reported as unverified, never as success.
2. **Conservative completion.** Three of four runs passed the hidden tests while the agent
   reported inconclusive or unverified. There are no false successes, but agent-side
   verification is currently pessimistic.
3. **Audit gap.** When every check in a file is rejected, the file content is not kept, so the
   B money rejections can be verified only from their recorded reasons. A small fix: keep the
   original check file in the task record.
4. **Generation cost.** Writing the checks is the largest new fixed cost on this hardware.

Hidden tests remained the only basis for scoring.
