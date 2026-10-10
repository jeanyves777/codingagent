# Coding Brain Desktop — backend agent integration contract

**For:** the agent developing the core Coding Brain system.  **UI branch/PR:** `ui/desktop-workspace-prototype`, PR #13.  **Isolation:** work in your own branch; do not merge, rebase, or overwrite PR #13 while core releases are being integrated.  This is a coordination handoff, not permission to bypass approvals or change Round Two.

## Shipped in the desktop workstream

- Native Windows/WebView2 desktop window, Windows `.exe` and preview Inno Setup installer.
- Left-anchored chat with separate user/brain avatars, timestamps, copy actions, balanced right explorer, native folder picker, readable diff previews and a functioning profile/settings menu.
- Private loopback UI server with a per-session token; selected-root-only safe, read-only file browsing; no arbitrary command string evaluation from user prompts.
- First-run Setup, AI Providers and Updates screens; official CLI sign-in handoff for Claude/Codex; detection cards for other services; no implicit API billing.
- Typed core-service task proposal, digest-bound approval, test, acceptance and cancellation path. Legacy versions gracefully explain missing `chat`, `new` and `install` commands rather than mapping conversation to `run`.
- Isolated tests; Windows EXE/installer packaging and self-test. Automated Chromium UI smoke exercises chat, avatar/header alignment, profile menu, explorer, onboarding, providers, and narrow-screen behavior.

The desktop EXE **does not install Coding Brain's own Python engine on a fresh computer**, and does **not** make an unreleased PR available as a stable update. Those are core release/bootstrap tasks, not tasks to paper over with UI mock behavior.

## Backend work required for a complete, working product

### 1. Finish and publish the dependency-ordered core releases

- 0.9.1 path/read-only-task hotfix is merged but the stable release still requires authorized tag/publishing.
- Integrate/test/publish memory (#3), multimodal (#5), live journal and snapshots (#7), guided installer (#8), goal-first creation (#10) and conversation (#12), in the appropriate dependency order.
- Prepare/activate the owner-approved release workflow (#11). Never bypass protected environments or required reviews. No release is considered usable by this desktop until installed on the target computer.
- Real versioned compatibility check: a desktop bridge must identify the installed **engine version and supported operations**, not infer readiness from the presence of an executable.

### 2. First-install from a clean Windows machine (tracked in issue #15)

- The **public desktop installer** should detect whether the core engine is absent, explain and request owner authorization, then run the **same verified, checksummed** bootstrap owned by the CLI installer. Offer `Local` or `Full` profile without a PowerShell prerequisite.
- Full profile should detect and, with explicit approval, provision Git, supported Python, Ollama and a compatible local model, WSL2/Docker Desktop, both sandbox images, Claude Code CLI, Codex CLI, knowledge, and optional browser/vision/OCR dependencies. Honor vendor license, UAC, official download integrity, restart and resume. Never silently change BIOS settings, delete WSL/Docker resources, or install new paid services.
- Provide **structured, resumable installation status** and a reliable `doctor --full` report for the UI; don't parse formatted CLI stdout. A detected Docker executable is not evidence the daemon/sandbox works.
- The UI installer and core updater need a shared compatibility/rollback contract. Existing projects, memory, models, CLI sign-in and config must survive engine updates. Code-sign the production Windows installer once approval is available.

### 3. Conversational global brain and project management

- Expose a typed `conversation.send(message, conversation_id, project_id?)` operation plus streamed *output* and activity events, with clarification/confirmation states. Plain `hello` must get a normal reply without a repository or coding task; questions must work with the local model, while no-model mode reports a truthful blocker.
- Expose `projects.list/register/select` and `projects.create(goal, name, root, stack)` from any starting directory with safe folder checks. No manual `cd`/`git init`/first commit from the user. Do not scan an entire home directory unprompted.
- Maintain conversation state across turns and projects; keep project-bound context isolated. `Chat`, `Run task` and `New project` UI modes must share the same global brain instance and rights without mixing their side effects.

### 4. Governed task lifecycle and live events

- Standardize a typed, versioned IPC/API contract for `tasks.start(goal,project)`, `tasks.get`, `tasks.events(after_seq)`, `tasks.approve(proposal_id,digest,decision)`, `tasks.stop`, `tasks.accept` and `tasks.resume`.
- Proposal approvals must bind to the **current exact digest** and be single-use. Review, sandbox testing, repair, premium escalation, acceptance, and creation of a safe Git branch must always go through the existing backend authority checks. Never make the UI an independent executor or bypass approvals with `--yes`.
- Stream real agent IDs, model/provider routes, plan decisions, stdout/stderr, tests, failures, escalation, budgets, snapshots and heartbeats from the shared event journal. Do not expose private hidden chain-of-thought or invent progress. Multiple windows/status readers must not interrupt a running task.
- Make cancellation and crash recovery safe; notify the desktop when the installed engine updates so it can reconnect/restart gracefully.

### 5. Provider registry and connections (tracked in issue #14)

Define backend provider descriptors with distinct fields: `installed`, `authentication`, `capabilities`, `enabled`, `readiness`, `billing_type`, `cost_gate`, `last_error`, and `supported_actions`. UI must not infer that merely installed means usable for execution.

| Provider | Initial integration | Authentication and cost policy |
| --- | --- | --- |
| Local Ollama / Qwen | Primary worker | Local model and generation self-test; no paid subscription |
| Claude Code | Optional planner/reviewer/escalation supervisor | Vendor CLI sign-in, existing subscription, configured per-task budget, account state never exposed in logs |
| OpenAI Codex | Optional planner/reviewer/escalation supervisor | Official Codex CLI `login`/ChatGPT sign-in; do **not** substitute billed API keys without separate opt-in |
| Google Gemini | New, reviewed governed adapter, only after capability/CLI tests | Verify official Gemini CLI authentication; distinguish subscription, free quota and API-billed paths |
| xAI Grok | Reviewed hosted adapter when implemented | API-key billing, explicit budget and per-provider cost consent; no implied subscription connection |
| Meta Llama | Select supported local models via Ollama; hosted endpoint only with separate adapter | Local model or separately authorized hosted API, not a fabricated universal Meta account login |
| "Mouse" / Muse | Placeholder pending vendor/product identification and actual documented SDK/auth path | **Do not** fake availability or login; require explicit identification and verification first |

Keep credentials in vendor CLIs or OS-protected stores; no passwords, tokens, key text, account IDs, or auth logs in UI output. The UI may offer `Connect` only for verified official flows and must label future adapters as unavailable, not active providers. The primary free model remains default; premium providers are used only by configured routing policy and within budgets.

### 6. Windows production acceptance matrix

Must demonstrate and capture evidence for the combined app on a real Windows 10/11 PC with WebView2:

1. Clean-machine UI installer can guide and verify the core full setup, including restart/resume, with an explicit opt-out for premium sign-in.
2. A user launches Coding Brain Desktop from Start without PowerShell, says `hello`, and receives a natural reply from the installed conversation engine without a task or Git repository.
3. User opens a folder on the right, previews text, switches projects, and cannot read `.env`, `secrets`, symlinks/junctions or outside-tree paths.
4. User asks the brain to create a new app globally; it creates a secure repository, validates a test scaffold, and reports honest goal-verification results; Qwen failures remain visible, not a fake green.
5. User runs a coding task: plan/diff → digest-bound approval → Docker sandbox → failure diagnosis/optional premium escalation → tests → accept a branch. Closing the UI must not lose or duplicate the task.
6. Claude and Codex subscription CLI connection and removal are detectable, never leak credentials, and never spend API-billed credits without authorization.
7. Other providers are presented truthfully; only working adapters may become selectable. Gemini/Grok/Meta/Muse must not be mislabeled connected merely because they have UI cards.
8. Real activity, snapshots, and multi-agent routing are observable, with a clear warning for stalled model calls; normal status inspection is read-only.
9. Stable engine update/rollback and desktop update leave projects, memories, credentials, and local models intact. Confirm binary integrity and packaging signature requirements.
10. Windows native GUI startup, resize, first-run modal, profile menu, right explorer, review/approval, dark/light accessibility (where supported), error recovery, and screen-reader keyboard use are verified beyond a headless self-test.

## How to integrate without collisions

1. Continue the core work only in its existing PR stack and resolve/review its releases normally.
2. When the typed backend API is stable, post its schema, supported engine version and test entry points on **PR #13** (and issue #9 if needed).
3. Bring the tested core head into a **temporary integration branch**; do not force-push or cherry-pick over PR #13's UI branch. Reuse the same task journal, provider router, memory, installer and updater; avoid parallel implementations.
4. Run deterministic CI gates (Windows, sandbox, security, API contract, installer) independently of stochastic model-capability reports. Keep failing model capability results visible and a blocker for claiming autonomous project creation production-ready.
5. Only after the full acceptance matrix passes should the owner approve merge/release. Maintain Round Two's frozen benchmark environment untouched.

**Owner expectation:** Coding Brain is a *global conversational coding system*. Projects are managed resources, not a prerequisite for opening or talking to the brain. Desktop work must not be merged merely because a nice screenshot or Windows self-test exists.
