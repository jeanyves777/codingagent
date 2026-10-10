# Coding Brain Windows updates: stable releases and opt-in previews

Coding Brain has two separately installed components: a Python **engine** and a native Windows
**desktop EXE**. Versions and upgrade results are never inferred from the UI appearance.

## Channels

- **Stable (default)** reads GitHub's latest published, non-prerelease `vX.Y.Z` release.
  The approved release workflow builds the wheel and, when the same reviewed commit contains
  the desktop, a matching `CodingBrain-Setup.exe`. Both must be present in the same release
  manifest; SHA256SUMS covers all assets. Older v0.9.0–v0.11.0 releases have **no native
  desktop installer**, so their engine-only updates cannot refresh the desktop EXE.
- **Preview (explicit opt-in)** is **not a GitHub Release and has no release tag**. It is a
  versioned `CodingBrain-Preview-X.Y.Z.devN` GitHub Actions artifact for the **current**
  `preview` branch head. Coding Brain only offers it if the exact head has a **completed
  successful** Windows Preview workflow with a unique unexpired artifact. The user must have
  `gh` installed and authenticated to download the artifact. A successful older head, red
  workflow, expired artifact, or an artifact without a matching desktop installer is refused.
  The installer and wheel must share one `release.json` version and SHA256SUMS.
  CI previews are development/testing builds, **not owner-approved production packages**.

The desktop's Updates tab offers the channels separately. Selecting Preview never changes the
default Stable channel. The selected channel is forwarded to the installed desktop's PowerShell
helper, which rechecks the source and requires explicit confirmation. A **preview currently
cannot be selected in already installed desktop builds** that predate this feature.

## Update sequence

1. The user selects a channel, then **Check for updates** (read-only).
2. The UI reports *installed engine*, *installed desktop*, latest candidate, and whether a
   matching native installer is actually available. It never labels a workflow artifact as a
   published GitHub release.
3. **Install latest** requires an affirmative in-app confirmation, opens a separate interactive
   PowerShell window, and asks the user to close the desktop.
4. The helper pins one stable GitHub tag or one completed successful preview run + head SHA,
   checks the release manifest and installer SHA256, and refuses to install an older engine
   version (PEP 440 ordering of e.g. `0.13.0.dev4` versus `0.11.0`).
5. If newer, Coding Brain's existing updater installs the exact engine version and backs up
   state. It runs migrations and an offline doctor gate before switching the active version.
6. Only after that succeeds is the matching native desktop installer run. The desktop installer
   updates EXE files and Start Menu shortcuts, but **not** engine state, models, or credentials.
7. The user restarts the desktop. Two-component failures are reported distinctly. There is no
   unattended installer execution, no in-place overwrite of an active EXE, and no automatic
   installation of an unreviewed preview.

The first unified version requires a **one-time native desktop reinstall**. Updating only the
Python wheel cannot retroactively install new desktop Update-button code.

## Shipping gates

Do not publish a full Windows application release or mark the feature ready until all pass:

- Windows native EXE packaged from the exact reviewed SHA, installed, self-tested, and
  uninstalled in a clean environment
- Matching wheel+desktop version, release manifest declaration, and all published checksums
- Stable ignores unreleased or prerelease CI runs; Preview never sees old/head-mismatched,
  unsuccessful or expired artifacts
- A real Windows desktop update click: Check → Confirm → close → verified engine update →
  native installer → restart → compare both installed versions
- Preserve Codex/Claude sign-in, Ollama models, project memory, task state and rollback
- No downgrade from a newer preview version to an older stable version without an explicit
  separate supported rollback path

The PR is development-only until these gates succeed. Do not merge the combined preview branch
into main merely to make an updater available.

## Live Git panel

The desktop explorer has a **Git** tab and a clickable branch label in the footer.
The tab shows the active branch (including detached or unborn HEAD), local branches,
recent 20 commits with author/date/hash, and staged/modified/untracked working-tree
counts with up to 75 changed-file names.

The panel reads **only the currently selected project** from the local authenticated
desktop bridge (`GET /api/git/snapshot`; callers cannot submit arbitrary file paths).
It uses `git --no-optional-locks` and never checks out a branch, stages a file,
creates a commit, fetches/pulls or pushes. It does not call GitHub and requires no GitHub
credentials. Commit subjects and filenames are displayed as literal text, never HTML.

When the desktop is visible and a project is selected, Git is rechecked every 3 seconds;
the branch indicator updates even when another explorer tab is selected. Switching
projects ignores stale in-flight Git results, and clicking Refresh updates immediately.
After an agent task ends the Git view refreshes again. Browser and real-repository tests
validate branch checkout, commits, rename/untracked handling and automatic changes.

This is **near-live polling**, not a persistent filesystem watcher or remote GitHub
event stream. Remote branch and PR tracking can be added independently in a later
reviewed feature without compromising the local-only Git panel.
