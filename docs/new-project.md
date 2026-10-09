# Start a new application from one sentence

```powershell
codingbrain new "Build a task manager with due dates and priorities"
codingbrain new "A recipe website with search" --name recipes --stack web
codingbrain new "A CLI that tracks expenses" --in D:\Code --yes
```

You do not create a folder, run `git init` or make a first commit. `codingbrain new` does that,
then builds your goal with the normal Coding Brain lifecycle and live activity.

## What it does

1. **Checks that this computer can build and test.** It needs:
   - Git;
   - the local model server and a model;
   - Docker;
   - the sandbox images.

   If any of these is missing, nothing is created, and you get the exact `codingbrain install`
   command that fixes it.
2. **Creates a new, empty folder**: `<your Projects folder>\<name>`, for example
   `C:\Users\you\Projects\task-manager`.
   - It never reuses or overwrites an existing folder.
   - It refuses a path that overlaps Coding Brain's own data.
   - It refuses a path outside the roots you allowed in `codingbrain setup`.
   - It refuses a path inside another Git repository.
   - It refuses a Projects folder that is a symlink or junction. It names the real folder so you
     can pass it with `--in`.

   You can change the default location with the `create.projects_root` setting.
3. **Sets up Git:**
   - It runs `git init` on branch `main` and makes an **empty baseline commit** authored by you.
   - If Git has no author identity on this computer, it asks for one (or takes `--git-name` and
     `--git-email`) and stores it **in the new repository only**. Your global configuration is
     never changed.
   - Without a terminal and without an identity, it stops with the two `git config` commands to
     run.
4. **Makes a scaffold commit.** It lists every file before writing anything:
   - `README.md` and `.gitignore`;
   - `coding-brain.json`, which selects the sandbox test profile (and the browser preview for web
     projects);
   - the test setup for the stack.
5. **Records your goal in the project's memory** as your approved instruction, together with the
   project's constraints.
6. **Builds the goal** through the usual steps:
   - a plan;
   - review;
   - an isolated worktree;
   - tests in the offline sandbox;
   - for web projects, rendering in a browser with layout and accessibility checks.

   Live activity is shown throughout. You accept the result, which creates a branch. Your `main`
   keeps the scaffold until you merge.

If anything fails before the scaffold commit, it removes only the folder it created.

## Stacks

| Stack | Chosen when the goal mentions… | Tests | Preview |
| --- | --- | --- | --- |
| `python` (default) | anything else | pytest (in the sandbox) | — |
| `node` | Node, JavaScript, TypeScript, npm, Express | `node --test` via `npm test` | — |
| `web` | web app, website, page, dashboard, frontend, HTML, UI | `node --test` for logic | `index.html` in a browser |

Override the choice with `--stack`.

## Dependencies

The sandbox has no network, so new projects start **dependency-free**:

- Python uses the standard library.
- Node uses its built-in test runner.
- Web projects use plain HTML, CSS and JavaScript.

This is recorded in project memory, so the model plans within it.

If a solution still needs third-party packages, the tests fail with the missing module. The
result then names the packages, taken from the sandbox output and not guessed, and offers two
ways forward:

- continue without them (`codingbrain resume`, asking for a standard-library or built-in
  solution); or
- acquire them with your explicit approval.

Network access for the sandbox is never granted quietly. A future `codingbrain deps` command will
download pinned packages with hashes into a project cache after you approve. Tests then install
from that cache offline, which keeps them reproducible. That command is not part of this release.

## After it finishes

`codingbrain new` ends in one of these states:

| State | What to do |
| --- | --- |
| **Accepted** | `cd` into the project and `git switch <branch>`. |
| **Passed, not yet accepted** | `codingbrain accept <task>`. |
| **Blocked or failed** | The message says why. `codingbrain trace` shows the full history, and `codingbrain resume` continues. |
