# Coding Brain on Windows: install once, use in any project, update in place

Supported: Windows 10 and 11 (64-bit), Windows PowerShell 5.1 or PowerShell 7, Python 3.11+,
Git for Windows. Optional: Ollama (local models), Docker Desktop (the offline test sandbox),
the Claude Code and Codex CLIs (premium supervision through your own subscriptions).

## 1. Install

Open PowerShell (no administrator rights needed) and run:

```powershell
$installer = "$env:TEMP\codingbrain-install.ps1"
Invoke-WebRequest https://github.com/jeanyves777/codingagent/releases/latest/download/install.ps1 -OutFile $installer
powershell -ExecutionPolicy Bypass -File $installer
```

The installer:

1. Finds Python 3.11+ and Git. If one is missing, it offers to install it with `winget`, and asks first.
2. Downloads the latest **published stable release**: the wheel, `constraints.txt` (exact
   dependency versions) and `release.json`. It checks each one against the release's
   `SHA256SUMS` and stops before installing anything if a checksum differs.
3. Installs that version into its own virtual environment:
   `%LOCALAPPDATA%\CodingBrain\app\versions\<version>`.
4. Runs the health check, writes the launcher `%LOCALAPPDATA%\CodingBrain\bin\codingbrain.cmd`,
   and adds that folder to your user PATH.

Options: `-Version 0.9.0` installs an exact release. `-Channel dev` installs the newest
pre-release. `-From <folder>` installs from release files you already downloaded.
`-InstallDir <path>` changes the location. `-Python <path>` chooses the interpreter.

Open a **new** terminal afterwards. PowerShell, Windows Terminal and VS Code terminals all pick up
the new PATH.

## 2. First-run setup

```powershell
codingbrain setup
```

Setup asks for:

- **Local model.** The Ollama URL (default `http://localhost:11434`); setup lists your installed
  models. Pick a main model and an optional faster one, for example
  `ollama pull qwen2.5-coder:7b`.
- **Other free providers.** Any OpenAI-compatible server, such as LM Studio, llama.cpp or a free
  hosted tier. For a hosted key you give the *name* of an environment variable (for example
  `GROQ_API_KEY`). The key itself is never written to disk by Coding Brain.
- **Claude / Codex.** Setup runs `claude auth status` and `codex login status` and reports what it
  actually finds:
  - signed in with a subscription;
  - signed out;
  - signed in with an API key — billed separately, so it is refused unless you allow it.

  Nothing is assumed to be unlimited. You choose whether to enable each one.
- **Premium budgets.** Plans and diagnoses per task, and a daily limit shared by all your projects.
- **Approvals.** Choose between:
  - `propose` (default): show each plan and ask before running it;
  - `auto`: run plans, but still ask before accepting any result.
- **Allowed folders.** Optionally restrict Coding Brain to project roots such as `C:\Projects`.
- **Sandbox.** Builds the two small offline Docker images that tests run in. This needs Docker Desktop.
- **Knowledge library** (optional). Imports the license-checked engineering skills.

Check everything at any time:

```powershell
codingbrain doctor            # Python, Git, state, configuration, Docker, Ollama, Claude/Codex
codingbrain doctor --offline  # local checks only
```

## 3. Use it in any project

```powershell
cd C:\Projects\MyApplication
codingbrain
```

Coding Brain recognizes the project before you give it a goal:

- the repository root and the current branch and status;
- languages, frameworks and dependency managers;
- documentation;
- existing build and test commands;
- earlier Coding Brain tasks for this project.

Each project's memory and sessions live in `%LOCALAPPDATA%\CodingBrain\data\projects\<project>`,
separately from every other project. Then you describe a goal (prefix it with `orchestrate:` for
multi-agent work).

For every goal, Coding Brain:

1. Plans the work with your local model. Claude or Codex is consulted only within your budgets,
   for complex goals or repeated failures.
2. Shows you the plan and the diff.
3. After you approve, applies the change in an **isolated Git worktree outside your project** and
   runs the tests in the offline Docker sandbox. It repairs failures and escalates when needed.
4. After you accept a tested result, creates a **new branch** `codingbrain/<goal>-<id>`.

Your checked-out branch and working files are never modified. You review and merge the new
branch yourself.

| Command | What it does |
| --- | --- |
| `codingbrain` | Interactive session in the current project |
| `codingbrain run "goal"` | One goal (`--orchestrate` for dependent assignments, `--yes` to run plans without asking) |
| `codingbrain init` | Recognize and register the project. Read-only. |
| `codingbrain status` | Project, configuration, models, premium budgets, recent tasks |
| `codingbrain tasks` | Every task for this project |
| `codingbrain resume [id]` | Continue the latest unfinished task, or the given one. Interrupted tasks start again from a fresh worktree. |
| `codingbrain accept [id]` | Accept a tested result as a new branch |
| `codingbrain memory ...` | Cross-agent project memory: scan, import, sync, status, show, conflicts ([details](project-memory.md)) |
| `codingbrain doctor` | Health and provider check |
| `codingbrain setup` | Change settings |
| `codingbrain update` / `rollback` | See below |
| `codingbrain --version` | Installed version |

**Pausing.** Press Ctrl+C or close the terminal; the task is saved. `codingbrain resume` continues it later.

**Uncommitted changes.** Commit or stash tracked changes first. Coding Brain starts from your
last commit and never touches your working files.

## 4. Update

```powershell
codingbrain update --check   # is a newer stable release available?
codingbrain update           # install it
```

The update steps:

1. Read the latest **published release** from GitHub. The stable channel never takes a branch
   head; `--channel dev` also considers tagged pre-releases, and `--version X` pins one release.
2. Refuse to run while a Coding Brain session or a task is active.
3. Download the release and verify its checksums.
4. Check upgrade compatibility (`min_upgrade_from`) and the required Python version.
5. Back up configuration, project memory, sessions and the premium ledger to
   `%LOCALAPPDATA%\CodingBrain\backups\`.
6. Install the new version **next to** the current one. Running code is never replaced.
7. Run the new version's state migrations and its health check.
8. Switch the launcher only if everything succeeded. On any failure, restore the backup and keep
   the current version.

Your projects, your branches, and the branches Coding Brain created are never touched by an update.

```powershell
codingbrain rollback                  # back to the previous version (kept installed)
codingbrain rollback --restore-state  # also restore the state backed up before the update
```

To use the dev channel permanently, run `codingbrain setup --channel dev`.

## 5. Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\CodingBrain\uninstall.ps1"
```

This removes the application, the launcher and the PATH entry. It keeps configuration and project
memory, so a later install picks up where you left off. Add `-RemoveData` to delete Coding Brain's
own data too. Your projects and the `codingbrain/...` branches are never removed.

## Where things live

| Path under `%LOCALAPPDATA%\CodingBrain` | Contents | Changed by updates? |
| --- | --- | --- |
| `app\versions\<v>\venv` | application versions, side by side | added, old ones pruned (current + 2 kept) |
| `app\current.json`, `bin\codingbrain.cmd` | active version and launcher | switched after a successful update |
| `config\` | settings, generated model configuration | only through backed-up migrations |
| `data\projects\<project>\` | per-project memory, tasks, worktrees, telemetry | only through backed-up migrations |
| `data\knowledge.sqlite3`, `data\supervision.sqlite3` | knowledge index, premium ledger | only through backed-up migrations |
| `backups\` | state backups taken before updates and rollbacks | added |

## Troubleshooting

- **`codingbrain` is not recognized.** Open a new terminal (PATH changes apply to new terminals),
  or run `"$env:LOCALAPPDATA\CodingBrain\bin\codingbrain.cmd"`.
- **"No free model is reachable".** Start Ollama, or the model server you configured, then run
  `codingbrain resume`.
- **Tests cannot run.** Start Docker Desktop and run `codingbrain setup --sandbox`.
- **Execution policy errors.** Use the `powershell -ExecutionPolicy Bypass -File ...` form shown
  above. It applies to that one command only.
