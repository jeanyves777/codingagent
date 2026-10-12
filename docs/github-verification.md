# GitHub verification

Running a project's full test suite locally can be heavy (Docker, dependencies, CPU). With GitHub
verification, a project's tests run on GitHub Actions instead. It is opt-in per project:

```
codingbrain verify on --trusted-ref main      # in the project folder
codingbrain verify status
codingbrain verify off                         # back to the local sandbox
```

## The governed flow

GitHub validation runs **in the background**. It never blocks the rest of the day's work. The only
waiting happens at the end, before a release, when every pull request has to be validated.

1. **Plan:** you approve the plan as usual. Nothing leaves your computer before that.
2. **Quick local checks, then GitHub:**
   - Coding Brain checks changed Python files for syntax errors locally. This takes seconds and costs
     no CI run. A failure goes straight to the repair loop.
   - It then snapshots the task's worktree into a commit, pushes it to `codingbrain/verify-<task>`,
     and starts `codingbrain-verify.yml` from the trusted ref.
   - It does **not** wait for the run. The task is ready for you, and the run is recorded on it.
3. **Accept:** accepting creates the local branch and a **draft pull request** into `pr_base`. That
   is never a merge. The accept prompt says plainly that the full tests are still running.
4. **Background watcher:** the desktop, or `codingbrain pr`, follows every open pull request. It
   watches the task's GitHub runs together with the PR's own checks. A run only counts for the PR
   if it tested exactly the PR head's files, apart from its own requirement-check files.
   - **All green:** *"All N checks are green on … Merge it?"* with **Merge / Not now**. It merges
     only after your yes.
   - **Something failed:** the failure log, read from Coding Brain's own run or from the PR's
     failed check, plus **Fix it**:
     - Coding Brain plans a repair from the **pull request's current code** (its head, fetched
       first), not from your project branch.
     - You approve the plan as usual.
     - The accepted fix is pushed to the **same pull request's branch**. It's a fast-forward, never a
       force push, and no second PR is opened.
     - The PR validates again in the background and you're asked again when it's green. The older
       task is marked superseded, so only the newest one is followed.
     - In the terminal, `codingbrain pr` offers the same Fix it.
5. **Release:** `codingbrain pr --wait` waits until no check is still running. It then asks about
   each green pull request, so the release contains only validated work.

To wait for GitHub inside each task instead, which lets the repair loop see remote failures
directly, set `"wait": true`.

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
  - every check and counted run succeeded;
  - there is at least one;
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
| `wait` | `false` (default): validate in the background; `true`: wait inside the task |
| `timeout_minutes` | how long a waiting task waits for a run (default 40) |
| `merge_method` | `squash` (default), `merge` or `rebase` |

The repository must contain `.github/workflows/codingbrain-verify.yml` on the trusted ref.

Engine API, behind the feature flag `tasks.pull_requests`:
- `pull_requests.list` returns the open PRs and their validation. It is what the watcher polls.
- `tasks.pull_request` returns one PR's state, plus `ask_merge` and `question`.
- `tasks.merge` takes the confirmed `head`.
