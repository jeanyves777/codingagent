# Coding Brain Workspace UI (isolated workstream)

This is a **functional browser-based Windows/Linux workspace prototype**, not yet a packaged Tauri/Windows native installer. It connects to the installed `codingbrain` CLI through a local loopback-only bridge without importing or changing the Coding Brain core service.

## Start

From the repository root (with its existing FastAPI and Uvicorn dependencies):

```powershell
# On the Windows PC with Coding Brain installed:
powershell -ExecutionPolicy Bypass -File .\Start-CodingBrainUI.ps1
```

Alternatively, if Python has FastAPI and Uvicorn available:

```powershell
python -m desktop_ui
```

The command opens the local interface in your browser. For the MVP, this is a local browser window, not yet a native Windows EXE. From anywhere, choose a project using the project selector or native folder picker. On CI/headless machines, use `python -m desktop_ui --no-open`.

## Available today

- Global chat panel and a separate, explicit **Run task** mode. The UI never routes greetings into `codingbrain run`.
- Right-hand folder tree with lazy expansion, UTF-8 file preview, and symlink, dot-directory, secret and traversal exclusions.
- Recent projects from the existing Coding Brain registry, without scanning the home folder.
- Existing CLI is launched without a shell. Stdout/stderr are streamed into the conversation and activity view with timestamps; process exit states are honest.
- Explicit approval controls for real CLI prompts and a stop control. Prompts are not silently auto-approved. **Important:** v0.9.x CLI does not present interactive prompts through a piped child process; task approval cannot complete through this bridge until the typed approval interface is integrated. On v0.9.x, this UI can display the plan, but cannot execute approval steps. Do not use `--yes` to bypass approvals.
- One active CLI session at a time in the MVP to avoid duplicate execution or mixed project contexts.
- No core architecture or database migrations are involved; no new agent router, memory store, sandbox or model engine.

## Compatibility boundaries

- On **v0.9.0**, `codingbrain chat` is not implemented. Chat will report the CLI error; it is **not** redirected to a coding task. Upgrade to the release containing the conversational CLI to enable real conversational responses.
- Native `codingbrain new`, durable task-event streaming, rich snapshots, typed approval events and unified global project management live on other branches. **Do not claim them integrated here.** The bridge can later invoke service APIs in place of text CLI parsing.
- This prototype uses the existing text CLI because the core service does not yet expose a stable, authenticated typed desktop transport. It must be replaced with the approved typed IPC/service interface before a desktop production release.
- Browser preview is local-only, bound to `127.0.0.1`, with an ephemeral in-memory bearer token. No arbitrary shell or remote HTTP access is implemented. Do not expose via a reverse proxy or remote network.
- File display is intentionally read-only; never browse secrets via this UI.

## Next integration steps

1. Rebase the UI workstream after the memory, multimodal, activity, installer, goal-first and conversational PRs land in dependency order.
2. Replace CLI output parsing with the approved backend event journal and structured approval API.
3. Add native Tauri packaging with a loopback/private IPC handshake and first-run installer integration.
4. Validate real Windows folder picker, long-running tasks, project switching, window closing, and upgrade/rollback compatibility.
5. Include screenshots from a Windows desktop, not rendered terminal transcripts, before publishing.

**Separate workstream rule:** This directory must not modify `brain/`, the frozen benchmark, existing PR branches, or release assets. All work stays in `desktop_ui/` and its own tests until approved integration.
