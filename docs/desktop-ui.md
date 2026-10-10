# Coding Brain Desktop — native Windows implementation

This workstream implements a **real desktop window** using Microsoft Edge WebView2 (via `pywebview`), not a browser-only mockup. The UI remains a thin client of the installed Coding Brain Python engine; it **does not bundle a second model, memory database, orchestrator or sandbox**. Source is isolated in PR #13, to be merged only after core-system integration and Windows verification.

## How it works

1. A native Windows window starts from `CodingBrainDesktop.exe` or `python -m desktop_ui.native`.
2. The window launches a private server **in the active installed Coding Brain Python environment** (`%LOCALAPPDATA%\CodingBrain\app\versions\<version>\venv\Scripts\python.exe`). It never executes user text in a shell.
3. Server binds an operating-system-selected port on `127.0.0.1`, returns a one-time 256-bit token to its parent via a private pipe, and accepts only authenticated API calls.
4. Chat uses the installed conversational Brain if available. Simple greetings work even on v0.9.0; older versions cannot answer general questions until the conversational backend release is installed.
5. Chat, new projects and coding goals all go through the **engine API** (`codingbrain api --stdio`, API 1.0; see [engine-api.md](engine-api.md)): safe Git worktree, requirement generation, proposal, exact diff, explicit user approval bound to the proposal's digest, isolated Docker tests, explicit acceptance on a new branch. No terminal prompt parsing and no `--yes`. Task activity is the engine's live journal for that task, plus truthful waiting heartbeats.
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

## One-click Windows installer (preview)

The native Windows CI now packages two options in the same build artifact:

- **`CodingBrain-Setup.exe`**: a per-user Inno Setup installer. It copies the native UI, creates a Start Menu shortcut, offers an optional desktop icon, and includes an uninstaller. It does **not** install or alter the Coding Brain engine or its dependencies.
- **`CodingBrainDesktop-Windows.zip`**: the portable application folder. Extract the complete folder and launch `CodingBrainDesktop.exe`; do not copy only the EXE.

Both packages have SHA-256 files alongside them. Verify the checksum before running them:

```powershell
$expected = ((Get-Content .\CodingBrain-Setup.exe.sha256) -split '\s+')[0]
$actual = (Get-FileHash .\CodingBrain-Setup.exe -Algorithm SHA256).Hash
if ($actual.ToLowerInvariant() -ne $expected.ToLowerInvariant()) { throw 'Checksum mismatch' }
Start-Process .\CodingBrain-Setup.exe
```

This is currently an **unsigned preview**. Windows SmartScreen may prompt. Do not override security warnings unless you trust the exact verified GitHub Actions artifact and understand the risk. Code signing and final Windows GUI/manual acceptance still remain before production publication. The installer is not yet distributed by `codingbrain update`.

The Windows CI validates the actual installer by installing it into a temporary per-user folder, running the installed native EXE self-test, then uninstalling it. This does not replace manual testing of the real graphical WebView2 window with the user's Ollama/Claude/Codex setup.


## First-run setup and provider control center (next desktop build)

The desktop first-run experience is now a guided **Control Center** with three tabs: **Setup**, **AI Providers**, and **Updates**. It does not assume that software found on PATH is proven operational. Basic preflight marks only detection. The diagnostic button detects what the installed CLI actually supports: newer backends run **Deep system test** (`codingbrain doctor --full`), while stable v0.9.0 uses a clearly labeled **Basic system check** (`codingbrain doctor`), never an unsupported flag.

The **Install & verify full system** button delegates to Coding Brain's official guided `codingbrain install --profile full`, including Docker, WSL, Ollama, its model, optional Claude/Codex CLIs, and other supported dependencies. It is only enabled if the installed backend exposes that command (introduced in the pending installer PR #8). Installation runs in a **visible Windows console**, with the backend installer retaining full vendor-license consent, elevation, restart/resume checks, and authentication interaction. There is **no silent `--yes`**, remote-script execution, or independent installation framework in the UI.

The **Updates** tab can check for stable updates, open the verified `codingbrain update` workflow, or launch `codingbrain setup --repair`. It does not pretend draft GitHub PRs are installable; version changes should be followed by an app restart. The UI is a separate app and does **not yet update its own desktop binary** through the Coding Brain updater. Desktop self-update requires a signed release artifact manifest, verification and an atomic replace/rollback contract before production activation.

**First-run state** is saved separately from Brain model/agent configuration under `.../CodingBrain/desktop/preferences.json`. Closing setup never makes OS changes. The next launch can still open Setup from the left rail or the top-right status indicator.

### Provider capability registry

The Control Center consumes backend provider metadata from `desktop_ui/management.py`. Provider status, sign-in, and real core integration are deliberately independent:

| Provider | Available integration | Authentication path | Core role today |
|---|---|---|---|
| Ollama / Qwen | Local main implementer | No subscription | Local worker |
| Anthropic Claude Code | Official CLI | Vendor's own interactive CLI sign-in | Budgeted supervisor (when enabled) |
| OpenAI Codex | Official CLI | `codex login` / Sign in with ChatGPT | Budgeted supervisor (when enabled) |
| Google Gemini | Official Gemini CLI detection | Vendor's own Google sign-in via CLI | Future governed adapter |
| xAI Grok | API key presence detection only | `XAI_API_KEY` environment variable | Not routed: API billing and approval policy required |
| Meta Llama | Local model option through Ollama | No general Meta subscription delegated through this UI | Future model/router selection |
| Meta Muse / “Mouse” | Research/extension slot | Unverified: do not invent developer sign-in | Not routed |

Every provider action is allowlisted; sign-in occurs in the official CLI (the desktop never sees passwords, API secrets, session cookies or tokens). Claude and Codex supervisor toggles call **existing `codingbrain setup --non-interactive`**; the React interface does not update any core provider config itself. The other providers are clearly identified as **integration pending** until a verified backend capability and permission/budget contract are implemented. Presence of an xAI key is never treated as sufficient authorization to send code to an API-billed service.

### Provider backend integration contract to implement after core release

Implement a capability-based `ProviderAdapter` extension in the global orchestrator, not the desktop. For each new adapter require `id`, `authentication_modes`, `capabilities` (chat/plan/review/code/vision), `cost_type` (local/subscription/API billed), `available`, `health`, `limits`, `run`, `cancel` and `audit` methods. Each invocation must be tied to task scope and explicit user-granted authority, model/provider budget, immutable routing evidence, secret-redacted logs, permission boundaries, and a fail-closed unavailable state. Use official supported remote/MCP/CLI integrations first; do not proxy subscription credentials into unofficial APIs. Do not replace the local builder by default. UI derives provider actions and status from real capabilities, not vendor names.

### Verification evidence and current limits

- 28 Python UI/management/native tests pass locally, including read-only project browsing, secure API authorization, approved maintenance commands, persistent first-run state, consent, vendor CLI handoffs, provider-status secrecy and core-task approvals.
- The Chromium mock-bridge interaction smoke tests first-run panel, seven provider cards, update controls, chat, project selection, and file preview with no browser JavaScript errors. These are UI/browser tests, **not** proof that the user's real installed backend supports all features.
- Existing published Coding Brain v0.9.0 does not have the full installer, goal-first creation or the conversational command, so those controls clearly report the installed-backend limitation until the respective release is published and installed. On older versions, `hello` remains a local lightweight greeting; complex conversational requests must wait for the conversational backend release.
- Native full GUI interaction on a Windows PC, full first-install on a clean Windows machine without preinstalled Coding Brain, support for connected Gemini/Grok/Muse execution, and signed desktop self-updates remain **not yet demonstrated**. A desktop installer alone does not bootstrap the absent core interpreter today; a separately verified installer integration is needed for that clean-machine scenario.


## Workspace layout and backend handoff (UI polish pass)

The conversation now uses **left-anchored message rows**, distinct user/brain circular avatars,
real per-message timestamps, sender identity, and a copy action. The composer uses the same
left content grid as the message bubbles; it no longer floats independently in the center.
The right-side explorer remains resizable by viewport and has functioning collapse/reopen
controls. The top-right **local profile** button opens a keyboard-dismissable popover with
project context and setup/provider entry points; it does not pretend to be a vendor login.

The browser-layout CI job runs real Chromium interactions at full-screen, 1366px laptop,
and narrow widths, checks alignment and page overflow, and uploads screenshots. That is
**browser evidence, not a substitute for a manual real-WebView2 Windows GUI test**.

## Engine API integration

The desktop owns no orchestration. `desktop_ui/engine_client.py` starts one engine process
(`python -m brain.local api --stdio`) from the installed Python and speaks the typed API;
`desktop_ui/core.py` maps the window's flow onto it:

| Window | Engine API |
| --- | --- |
| Chat | `conversation.send` (never executes; proposed work is shown with how to authorize it) |
| New project (after approval in the window) | `projects.create`, then the task flow with `new_project` |
| Run task | `projects.register`, `tasks.start` |
| Approve or decline the proposal | `tasks.approve` with the digest of the proposal that was shown |
| Protected tool request | `tasks.tool_decision` with the request id |
| Accept | `tasks.accept` (a new branch; the checked-out branch is unchanged) |
| Stop | `tasks.stop` over the same engine session; the running step ends `cancelled` |
| Activity | the task's live journal events (`op_id`-scoped), heartbeats when quiet |

Readiness (`GET /api/engine/status`) comes from the installed engine: with API 1.0 installed, the
version comes from `engine.info` and Claude/Codex readiness from `providers.list`, run by the
installed engine's interpreter (never a module bundled into the UI). The CLI probes remain only for
older installed engines such as v0.9.0, where chat is reported unavailable and refused, with no
canned greeting, and protected execution never starts.

If the engine process dies, the next call starts a new one from the persisted state; a task whose
engine died is reported as interrupted, never as passed. Tests: `tests/test_desktop_engine.py`
(the window's HTTP endpoints, the desktop core and the real engine together, with a fake model
and sandbox; and the out-of-process client against a real engine process).

For the core/backend agent, see **[`docs/backend-integration-handoff.md`](backend-integration-handoff.md)**.
It defines the remaining clean-machine installation/bootstrap, stable updater, true global
conversation, multi-provider backend adapters, typed activity and task lifecycle, and
Windows end-to-end verification contracts. Those gaps are **not claimed implemented in the UI**.

## Truthful async loading and progress (desktop UI)

The setup window, provider actions, updater, and chat composer expose async states. The UI does **not** invent percentages, fake stages, or mark launched external terminals as completed installations.

- First-run/readiness probe: `control-feedback` announces the probe and elapsed time, plus accessible skeletons in the component grid. Refresh is disabled while its request is pending. The full-install button is unavailable until capability discovery confirms it is supported.
- System actions: only explicitly confirmed operations launch. While an API-backed operation is pending, the corresponding button is disabled and displays a spinner. The UI polls the backend job, shows real output, and releases the button on success or failure. When an official terminal opens, the UI states **launched**, not **completed**, and directs users to finish in that terminal.
- Provider sign-in: opens official CLI after approval, shows a temporary launching state, and asks the user to refresh authentication status afterward. No password/token/session content is captured.
- Chat: a typing message and spinner appear only while a request or task is outstanding, and are removed when a backend output arrives or execution ends. Repeated Enter/send cannot start duplicate work.
- Failures: connectivity errors expose an error state; after three polling failures the UI warns the operation might still be active rather than claiming cancellation. No automatic retry initiates a second installer or coding task.

Regressions: `python tests/smoke_loading_browser.py` simulates deliberately unresolved API requests in Chromium and verifies live loading, resolution, failure, retry, duplicate-submit prevention, and zero JavaScript exceptions. It runs alongside `tests/smoke_browser.py` in `.github/workflows/desktop-windows.yml`.

## Desktop-native approvals and legacy diagnostic support

The Control Center and task stop/AI-provider controls never use browser-origin `window.confirm`, `window.alert` or `window.prompt`. They display a Coding Brain `<dialog>` with explicit action and Cancel buttons. Default keyboard focus is Cancel. Escape, dismissing the backdrop, and Cancel all decline; no protected endpoint is called unless the user affirmatively selects the action. The backend still requires explicit `{confirmed:true}` for system changes and provider sign-in. This is a user-interface improvement, **not** a bypass of the engine's existing digest-bound plan approvals.

Older Coding Brain installations lack `doctor --full`; desktop probes `codingbrain doctor --help` without running a real test to detect whether the option is supported. On older versions, the UI labels the command **Basic system check** and submits only `codingbrain doctor`, reporting it as a basic check even if it passes. Full model and sandbox readiness remain unverified pending a backend upgrade. On newer installations the deep command is used. The backend itself performs the option check, so calling the local UI API directly cannot force an unsupported `--full` flag.

Regression evidence: `python tests/smoke_confirm_browser.py` rejects all native browser dialogs and exercises explicit confirmation, Escape/Cancel refusal, provider sign-in, and legacy doctor labeling. `tests/test_desktop_management.py` tests both capability branches, option probing through `--help`, and allowlisted maintenance commands.


## Subscription provider connection and rechecks

The provider screen distinguishes **CLI installation**, **vendor authentication**, and
**Coding Brain supervisor routing**. Claude or Codex is labeled `Connected` only when
its *official CLI status check succeeds* and the supervisor is enabled in the
Coding Brain configuration. A CLI that is signed in but disabled is labeled
`Authenticated — supervisor disabled`; installed without verified CLI auth is
never claimed connected. The screen provides **Recheck connections** and a
specific **Recheck connection** action for authenticated providers rather than
asking users to sign in again. After an explicit launch of an official CLI
sign-in, read-only status checks repeat for up to two minutes while the panel
is open; launching a terminal is never treated as successful authentication.

Windows packaged applications sometimes inherit PATH from before npm/winget
added CLI shims. Provider discovery first uses PATH and then checks fixed
per-user npm shims and WinGet links; this does not run external executables to
find them and does not read or store account tokens. If a CLI cannot be found,
restart the desktop after installing it, then select **Recheck connections**.

Test: `python tests/smoke_provider_browser.py` and
`python -m pytest -q tests/test_desktop_management.py`.


## Installed engine and model readiness (desktop integration)

The green **Desktop connected** footer confirms only the loopback UI bridge,
**not** that the installed Coding Brain engine, its local Qwen model, or its
premium supervisors are ready. The header checks the **installed** engine
through read-only CLI capability probes (`codingbrain version`,
`codingbrain chat --help`, `codingbrain api --help`); the model pill shows the
configured model from the installed engine's own configuration. Ollama model
availability comes from local `http://127.0.0.1:11434/api/tags`, not merely
the presence of `ollama.exe`. No inference requests, remote network calls or
system changes occur during these probes. A detected model still requires a
deep doctor/self-test to demonstrate actual generation.

A v0.9.0 engine cannot handle ordinary questions through the new chat
interface. The UI shows a non-green warning and an Update link rather than
pretending the local bridge is a connected brain. The conversational/API PRs
must be merged, released and installed before general chat is available on a
normal Windows installation.

Claude Code and Codex each display independent readiness: CLI found, official
sign-in verified, and supervisor enabled in Coding Brain's configuration.
An authenticated premium supervisor is **not** automatically the primary
chat model. The composer displays distinct provider chips without exposing
account credentials.

**Backend integration requirement for PR #17:** Prefer typed `engine.info`
and `providers.list` over CLI capability probes once the versioned engine API
is shipped; preserve the honest UI-facing fields and human approvals. The
Windows acceptance test must cover a real Qwen inference turn, authenticated
premium supervisors, and recovery after upgrading an older engine.
