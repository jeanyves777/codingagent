# GitHub verification

Running a project's full test suite locally can be heavy (Docker, dependencies, CPU). With GitHub
verification, a project's tests run on GitHub Actions instead. It is opt-in per project:

```
codingbrain verify on --trusted-ref main      # in the project folder
codingbrain verify status
codingbrain verify off                         # back to the local sandbox
```

## The governed flow

1. **Plan:** you approve the plan as usual. Nothing leaves your computer before that.
2. **Verify:** Coding Brain checks changed Python files for syntax errors locally, which takes seconds
   and costs no CI run. It then snapshots the task's worktree into a commit and pushes it to
   `codingbrain/verify-<task>`. It starts `codingbrain-verify.yml` from the trusted ref with that
   commit. A failure log goes to the repair loop, exactly like a local sandbox failure.
3. **Accept:** you accept the result. Coding Brain creates the local branch and opens a **draft pull
   request** into `pr_base`, with the goal, plan, files and test evidence.
4. **Merge:** Coding Brain watches the pull request's checks. Only when **every check is green** does it
   ask you: *"All N checks are green on … Merge it?"*. It asks in the desktop with Merge / Not now, or
   in `codingbrain pr <task> --wait`. It merges only after your yes.

## Fixed rules (governance)

- Nothing is pushed before you approve the plan.
- Coding Brain pushes only to `codingbrain/*` branches, never to `main` or the PR base.
- A change that touches `.github/` is never pushed: CI stays human-controlled.
- The workflow definition always comes from the **trusted ref**, a branch only humans change. Only
  the code under test comes from the agent's commit. The workflow has `contents: read`, no secrets,
  and `persist-credentials: false`. Its inputs are validated: the ref must be a full SHA, and the
  tests must be paths or node ids.
- The worktree, its index and HEAD are never touched by verification. Requirement checks travel only
  inside the verification commit.
- A merge is refused unless:
  - every check succeeded;
  - there is at least one check;
  - the PR is mergeable;
  - the head is still the commit you were asked about (`--match-head-commit`).
  A new push means a new question. Branch protection is never bypassed (no `--admin`).
- GitHub access is your signed-in `gh` CLI. Coding Brain stores no token.

## Settings

The settings live in your Coding Brain `config.json`, outside the repository, so the model cannot
change them. They sit under `verification.projects["<project path>"]`:

| key | meaning |
| --- | --- |
| `trusted_ref` | branch the workflow is read from (default `main`) |
| `pr_base` | branch pull requests target (default: the trusted ref) |
| `tests` | test paths to run (default: the whole suite) |
| `timeout_minutes` | how long to wait for a run (default 40) |
| `merge_method` | `squash` (default), `merge` or `rebase` |

The repository must contain `.github/workflows/codingbrain-verify.yml` on the trusted ref.

Engine API: `tasks.pull_request` returns the checks, plus `ask_merge` and `question`. `tasks.merge`
takes the confirmed `head`. Both are behind the feature flag `tasks.pull_requests`.
