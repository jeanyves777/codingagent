# Durable cross-agent project memory (0.10.0)

Coding Brain learns what a project and its earlier coding agents already established, keeps it
with provenance, and carries it across sessions, restarts and updates, so you don't have to
explain the same rules again.

## What it reads

| Source | Examples | Imported |
| --- | --- | --- |
| Project agent instructions | `CLAUDE.md`, `CLAUDE.local.md`, `.claude/rules/`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `.github/copilot-instructions.md`, `.github/instructions/`, OpenCode `opencode.json` instructions, `GEMINI.md`, `.windsurfrules`, `.clinerules`, `CONVENTIONS.md` | automatically |
| Project documents | README, `ARCHITECTURE.md`/`DESIGN.md`, ADRs (`docs/adr/`, `decisions/`), `TODO.md`, `ROADMAP.md`, plans, `CHANGELOG.md`, `docs/*.md` | automatically |
| History | recent Git commits and branches; Coding Brain's accepted work and failed attempts | automatically |
| Private agent memory for this project | Claude Code session summaries and memory (`~/.claude/projects/<project>/`), Codex sessions whose working directory is this project (`~/.codex/sessions/`), OpenCode sessions | **only after you authorize each source** |
| Account-wide agent instructions | `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.config/opencode/AGENTS.md`, `~/.gemini/GEMINI.md` | only after authorization, into global memory, and used in projects only once you approve a rule |

Only local files are read. Agents that keep their history somewhere Coding Brain can't read as
files, such as Cursor chat history, are reported as not covered. Nothing is fetched from accounts,
no access restriction is bypassed, and files that look like secrets (`.env*`, `*secret*`,
`*credential*`, keys) are never opened, including when a custom instructions glob (OpenCode)
matches them: the name is checked before anything is read or hashed.

Project-local sources are imported automatically only if they are regular files inside the
project reached without a symbolic link or junction. A `CLAUDE.md`, rule file or docs folder that
links outside the project is skipped, so text from elsewhere (including your private agent memory)
never enters project memory without the authorization those sources require. The check runs at
discovery and again just before each file is read, and the opened file must be the one that was
checked.

## Three scopes

- **Global:** `data/global-memory.sqlite3`, holding rules you approved for every project.
- **Project:** `data/projects/<project>/memory.sqlite3`, one isolated store per project. A store
  refuses writes for another project.
- **Task and session:** the existing task store, holding goals, plans, changed files, tests,
  failures and checkpoints.

All of these live under `%LOCALAPPDATA%\CodingBrain\data`, outside the application. They are
backed up before every `codingbrain update` and kept by uninstall unless you pass `-RemoveData`.
Each store has a versioned schema, which the new version migrates during an update.

## Provenance, trust and conflicts

Each record keeps:

- the source agent or document, the file and line (or session);
- the import time and the original modification time;
- the project and a content checksum;
- a verification status (`unverified`, `verified`, `verified_in_code`, `not_found_in_code`,
  `possibly_done`, `approved`);
- whether it is active, superseded or removed.

When records disagree, the lower authority level wins:

1. Coding Brain's security and authorization boundaries (fixed).
2. Your current instructions and rules you approved.
3. Verified current project state (Git history; work that passed the sandbox tests and that you accepted).
4. Project rule files and accepted ADRs.
5. Historical agent decisions, summaries, plans and sessions.
6. Unverified imported notes.

- **Reconciled with the code.** Claims of completed work are checked against the files and
  identifiers in the repository. A claim with no matching code is reported as not found, not
  treated as done.
- **Conflicts surfaced.** Conflicting rules or decisions (for example "use PostgreSQL" vs "use
  SQLite") are listed for you to resolve. Until you decide, both reach the model marked as
  unresolved.
- **Imported text is data.** It reaches the model only as reference material with its source and
  authority level.
- **Prompt injection quarantined.** Text that looks like injection ("ignore previous
  instructions", piping a download into a shell, sending data to a URL) is kept out of model
  context.
- **Secrets redacted** before anything is stored.

## Commands

```powershell
codingbrain memory scan        # sources found, their scope and whether they changed or need authorization
codingbrain memory import      # import project-local sources; --source <id> or --all-private to authorize more
codingbrain memory sync        # re-import only sources whose content changed
codingbrain memory status      # coverage, verification, conflicts, quarantined and redacted counts
codingbrain memory show        # the project profile: purpose, architecture, rules, decisions, completed and open work...
codingbrain memory conflicts   # list; resolve with --resolve <id> --keep a|b|both
codingbrain memory forget <id> # remove an incorrect record (kept in the audit log)
codingbrain memory approve <id>  # promote a record to an approved project rule
codingbrain memory rule "text" [--global]   # add your own rule
codingbrain memory contribute --agent codex --file review.md   # another agent's findings, as unverified history
```

- **First launch in a project.** `codingbrain` (or `codingbrain run`) discovers sources, imports
  the project-local ones, asks before importing private agent memory, reconciles with the code,
  and shows a continuation summary. The summary covers the purpose, architecture, rules,
  decisions, completed and claimed work, open tasks, known bugs and repair attempts, conventions,
  rejected approaches and conflicts.
- **Later launches** sync only what changed.
- **During development,** accepted results are recorded as verified changes and failed attempts as
  repair history. Generated assumptions are never promoted to facts without verification or your
  approval.
