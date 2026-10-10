# Coding Brain Desktop — native Windows implementation

This workstream implements a **real desktop window** using Microsoft Edge WebView2 (via `pywebview`), not a browser-only mockup. The UI remains a thin client of the installed Coding Brain Python engine; it **does not bundle a second model, memory database, orchestrator or sandbox**. Source is isolated in PR #13, to be merged only after core-system integration and Windows verification.

## How it works

1. A native Windows window starts from `CodingBrainDesktop.exe` or `python -m desktop_ui.native`.
2. The window launches a private server **in the active installed Coding Brain Python environment** (`%LOCALAPPDATA%\CodingBrain\app\versions\<version>\venv\Scripts\python.exe`). It never executes user text in a shell.
3. Server binds an operating-system-selected port on `127.0.0.1`, returns a one-time 256-bit token to its parent via a private pipe, and accepts only authenticated API calls.
4. Chat uses the installed conversational Brain if available. Simple greetings work even on v0.9.0; older versions cannot answer general questions until the conversational backend release is installed.
5. Running a coding goal calls the **existing Brain service**: safe Git worktree, requirement generation, proposal, exact diff, explicit user approval, isolated Docker tests, explicit acceptance on a new branch. This path uses typed Python API calls instead of brittle terminal prompt parsing or `--yes`. Task activity comes from real stored engine events plus truthful waiting heartbeats.
6. Goal-first project creation is supported if the installed Brain version includes it, and also requires explicit approval. It never overwrites an existing project path.
7. Project file browsing is read-only, bounded, refuses symlinks/junctions, path escapes and common credential files.

## Windows development launch (from a Coding Brain checkout)

The existing Coding Brain CLI must be installed first. In PowerShell **inside this repository**:

```powershell
.\Start-CodingBrainDesktop.ps1 -InstallDesktopDependency
```

The script installs **only the optional `pywebview` UI dependency**, and only when you explicitly request it. It runs in the current version's Coding Brain Python environment. Later launches need only:

```powershell
.\Start-CodingBrainDesktop.ps1
```

`Start-CodingBrainUI.ps1` remains available as a browser-mode fallback for machines without WebView2.

## Native Windows binary

GitHub's `Coding Brain native desktop (Windows)` workflow builds the executable using PyInstaller. If it succeeds, the workflow publishes a `CodingBrainDesktop-Windows.zip` artifact with `CodingBrainDesktop.exe` and its supporting DLLs/files, and a SHA-256 checksum. Extract the **whole** archive; do not copy the EXE by itself. The build uses the WebView2 renderer; Windows 10 machines may need the official WebView2 Runtime installed.

For a verified local artifact extracted to `C:\Downloads\CodingBrainDesktop`, optional per-user installation creates a Start Menu shortcut:

```powershell
.\installer\windows\Install-CodingBrainDesktop.ps1 -SourceFolder 'C:\Downloads\CodingBrainDesktop' -Version '0.1-dev'
```

The installer refuses an existing version rather than replacing it, and does not modify Python, Git, Docker, WSL, Ollama, Claude or Codex. Verify the CI artifact checksum before installing. This UI is not yet included in `codingbrain update`, because it has not passed final integration, code review and the user's Windows acceptance test.

## Current functionality

- Native desktop window with modern dark chat workspace and **right-side** expandable file explorer.
- Global chat with no project selected; optional project chooser and recent projects.
- Chat, Run task, and New project modes, with no conversion of a greeting into a coding job.
- Real task-stage activity and waiting heartbeats; no invented percentage or hidden model reasoning.
- Exact diff preview; approval is tied to one proposal ID and cannot be reused for a different request.
- Safe cancellation requests; code and acceptance are never silently approved.
- Separate local engine process, started from **the current installed version**, so core package updates are not frozen into the desktop EXE.

## Verification and remaining integration gates

Local (Linux development host): `python -m pytest -q tests/test_desktop_ui.py tests/test_desktop_native.py` currently passes **17 tests** including a real loopback startup/shutdown, authentication, file boundaries, typed approval/accept lifecycle against a controlled Brain adapter, and cancellation. `node --check desktop_ui/static/app.js` passes. A prior browser interaction test exercised chat, project selection and preview with a mock engine.

**Still required before shipping as production-ready:** GitHub Actions must successfully build and self-test the Windows executable; manual Windows GUI smoke with actual WebView2 and a real installed `qwen2.5-coder:7b`; successful real-model task in Docker with typed approvals and branch acceptance; long-run recovery; latest installed conversational and goal-first releases; source review, threat model, and user approval. Windows GUI is not verifiable from Linux screenshots or Windows runner headless tests alone. No release merge/publication or changes to the frozen benchmark are part of this PR.

## Windows one-click installer (unsigned preview)

The Windows desktop CI publishes both a portable application ZIP and CodingBrain-Setup.exe, a per-user Windows installer built with Inno Setup. The installer creates a Start Menu shortcut, optionally a desktop shortcut, and an uninstaller. It does not install or modify Coding Brain, its models, or its dependencies.

For the installer, verify the SHA-256 from CodingBrain-Setup.exe.sha256 with PowerShell Get-FileHash before running the downloaded executable. This preview is not code-signed; SmartScreen may warn. Never ignore unverified security warnings. The Windows CI installs the package into a temporary user directory, launches its self-test, then uninstalls it. This headless test does not demonstrate real GUI operation on the user's machine; WebView2, model/sandbox integration, and end-to-end UI approvals still require Windows acceptance testing. The UI is not yet distributed through codingbrain update.
