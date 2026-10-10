# Installation, dependencies and readiness

Coding Brain needs more than itself: Git, a local model server and a model, and, for testing your
code before you accept it, Docker with its sandbox images. Claude Code and Codex are optional and
use your own subscriptions. The installer prepares all of this, asks before each change, and tells
you exactly what works afterwards.

## Quick start (Windows)

```powershell
# Coding Brain, then Ollama and a model sized for this computer
powershell -ExecutionPolicy Bypass -File install.ps1 -Local

# Everything: also WSL 2, Docker Desktop, the test sandbox, Claude Code, Codex,
# the knowledge library, OCR, a vision model and a browser for visual checks
powershell -ExecutionPolicy Bypass -File install.ps1 -Full

# Only show the checks and the plan; change nothing
powershell -ExecutionPolicy Bypass -File install.ps1 -Full -PlanOnly
```

`install.ps1` installs Python and Git if they are missing, installs the verified Coding Brain
release, then hands over to `codingbrain install`. You don't need the `codingbrain` command
for this step. Once Coding Brain is installed, use the CLI directly:

| Command | What it does |
| --- | --- |
| `codingbrain install` | Prepare the **local** profile (or the profile you used last). |
| `codingbrain install --full` | Prepare everything. |
| `codingbrain install --plan` | Run the preflight and show the plan. Changes nothing. |
| `codingbrain install --resume` | Continue after a restart or an interruption. |
| `codingbrain install --only docker` | One component. Naming a component asks about it again. |
| `codingbrain install --retry-declined` | Ask again about components you declined earlier. |
| `codingbrain setup --repair` | Check every component in full and offer a fix for anything that does not work. |
| `codingbrain doctor` | Quick checks plus the readiness level, using saved evidence. |
| `codingbrain doctor --full` | Real checks: the model generates text, the sandbox images start offline, and Claude/Codex report their sign-in state. |
| `codingbrain doctor --json` | The same, as JSON (`readiness.level`, `readiness.levels`, `readiness.components`). |
| `codingbrain selftest` | A disposable end-to-end task (see below). |
| `codingbrain watch --install` | Follow a running installation from another terminal. |

## Profiles

| Component | Local | Full | Source |
| --- | :-: | :-: | --- |
| Python 3.11+ | ✓ | ✓ | winget `Python.Python.3.12` (installed by `install.ps1`) |
| Git | ✓ | ✓ | winget `Git.Git` |
| WSL 2 | | ✓ | Microsoft: `wsl --install --no-distribution` (needs administrator rights and usually a restart) |
| Docker Desktop | | ✓ | winget `Docker.DockerDesktop` (needs administrator rights; Docker's licence terms apply) |
| Ollama | ✓ | ✓ | winget `Ollama.Ollama` |
| Local coding model | ✓ | ✓ | `ollama pull` (Ollama verifies every layer's SHA-256) |
| Claude Code *(optional)* | | ✓ | winget `Anthropic.ClaudeCode`, then the official sign-in |
| Codex CLI *(optional)* | | ✓ | npm `@openai/codex` (and Node.js LTS through winget if it is missing), then `codex login` |
| Sandbox images | | ✓ | Built locally from Coding Brain's Dockerfiles (`python:3.11-slim`, `node:22-slim`) |
| Knowledge library *(optional)* | | ✓ | Licence-checked sources (`knowledge.example.json`) |
| Tesseract OCR *(optional)* | | ✓ | winget `UB-Mannheim.TesseractOCR` |
| Vision model *(optional)* | | ✓ | `ollama pull` (`qwen2.5vl:3b` or `:7b`, depending on memory) |
| Browser *(optional)* | | ✓ | Microsoft Edge (already part of Windows), or Playwright's Chromium |

Components are installed in this order. A component that needs another one is marked *waiting*.
For example, the sandbox waits for Docker, and Docker waits for WSL 2.

## What happens, step by step

1. **Preflight.** The installer reads:
   - the Windows version and build, architecture, memory, free disk and GPU;
   - whether you are an administrator;
   - whether winget is available, and the PowerShell execution policy;
   - network reachability (GitHub, the Ollama library, Docker Hub);
   - which proxy variables are set (only their names are recorded);
   - several virtualization signals.

   A drive with too little free space stops the installation. Everything else is a warning.
2. **Plan.** Each component's state comes from a real check, not from finding a file. The plan
   shows what would be done, where it comes from, the download size, whether Windows will ask for
   administrator rights, and the licence terms you should know about.
3. **Permission.** The installer asks before each change. `--yes` agrees to the plan it has just
   shown you. Windows still shows its own UAC prompt for every administrator change. A component
   you decline is remembered and not asked about again. Declining is a choice, not a fault, so it
   never makes the result "Degraded".
4. **Install and verify.** Each step is checked again after it runs. Long steps show the tool's
   own output (for example, "ollama: pulling … 1.20 of 4.68 GB" as reported by Ollama) and the
   elapsed time. When a tool is quiet, a heartbeat says so. The installer never estimates
   percentages.
5. **Readiness.** At the end the installer runs the deep checks and reports your level.

### Restarts and interruptions

- Progress is saved after every step in `%LOCALAPPDATA%\CodingBrain\install\state.json`.
- Running the installer again checks everything again and skips what is already done. A step
  that was interrupted is checked again rather than assumed to be complete.
- If WSL 2 needs a restart, the installer stops there. It offers to continue automatically once,
  after you sign in again. This uses a per-user `RunOnce` entry, which Windows removes after
  running it. Otherwise, run `codingbrain install --resume` yourself.
- Only one installation can run at a time.

### Virtualization

Windows reports *firmware virtualization: off* whenever a hypervisor is already running (Hyper-V,
WSL 2, Docker Desktop). So that flag alone never means virtualization is disabled. The installer
combines several signals: a running hypervisor, working WSL 2, a Docker engine running Linux
containers, and the processor flags. It reports one of:

- *enabled*;
- *possibly disabled*, with how to check in Task Manager > Performance > CPU;
- *unknown*.

It never reports virtualization as definitely disabled, and it never changes BIOS/UEFI settings.
If virtualization really is off, you turn on Intel VT-x or AMD-V/SVM in your firmware settings,
then resume.

### Docker

The Docker command being installed is not enough. The installer checks that the **engine** is
running **Linux containers**. Then it builds the sandbox images and starts each one with
`--network none` as a smoke test.

- If Docker Desktop is installed but stopped, the installer starts it and waits (up to 5 minutes).
- On its first start, Docker Desktop shows its own agreement. You answer it there; Coding Brain
  never accepts it for you.
- If Docker is in Windows-containers mode, you are told how to switch.
- Existing WSL distributions, Docker images, containers and volumes are never deleted.

### Local model

The model is chosen from your memory, or your GPU memory if that is larger:

| Memory | Model | Download |
| --- | --- | --- |
| under 8 GB | `qwen2.5-coder:1.5b` | about 1.0 GB |
| 8–15 GB | `qwen2.5-coder:3b` | about 1.9 GB |
| 16–31 GB | `qwen2.5-coder:7b` | about 4.7 GB |
| 32 GB or more | `qwen2.5-coder:14b` | about 9.0 GB |

The alternatives are shown, and a model you already chose in `codingbrain setup` is kept. Free
space is checked before downloading. A downloaded model does not count as working until it has
generated text: the generation test sends a short prompt and records the tokens and the time.

### Claude Code and Codex (optional)

These use your own subscriptions (Claude Pro/Max, ChatGPT Plus/Pro/Business/Enterprise). Coding
Brain never creates accounts and never switches you to API-key billing.

The installer reads the sign-in state only through each CLI's own status command
(`claude auth status`, `codex login status`). It never opens their credential files.

| State | Meaning |
| --- | --- |
| `not_installed` | The CLI is not on this computer. |
| `not_authenticated` | Installed, not signed in. |
| `authenticated` | Signed in with a subscription. |
| `api_key_billing` | Signed in with an API key. This is billed separately, so it is not used. |
| `expired` | The sign-in needs renewing. |
| `temporarily_unavailable` | The check could not reach the service. Nothing is changed. |
| `disabled` | Signed in, but turned off in your settings (`codingbrain setup --enable-claude`). |

When you agree to sign in, the installer starts the vendor's official flow (`claude auth login`,
`codex login`) in your terminal, and it opens your browser. API-key environment variables are
removed from that process, so a subscription sign-in is not replaced by key billing. Coding Brain
never sees or stores passwords or tokens.

## Readiness levels

`codingbrain doctor` no longer says "Healthy". It reports a level instead:

| Level | Means |
| --- | --- |
| **Core ready** | Python, Git, Coding Brain's state, and a local model that has generated text. Coding Brain can plan and write code. |
| **Sandbox ready** | Core, plus the Docker engine (Linux containers) and sandbox images that started offline. Your tests run before you accept anything. |
| **Hybrid ready** | Sandbox, plus Claude Code or Codex signed in with a subscription and turned on. |
| **Full ready** | Hybrid, plus the knowledge library, OCR, a vision model and a browser for visual checks. |
| **Degraded** | Something you set up or turned on is not working now (for example, Docker installed but its engine stopped, or an expired sign-in). The level reached is still shown. |
| **Blocked** | A core requirement is missing or failing. Coding Brain cannot work on goals yet. |

A level counts only verified evidence. The quick `doctor` uses the evidence saved by the last
`doctor --full` or installation, and says when something has not been verified yet.

## The end-to-end self-test

`codingbrain selftest`, or the offer at the end of `codingbrain install`, runs a disposable task:

1. It creates a throwaway Git project in your temp folder: a calculator with a bug, and a failing
   test.
2. It gives your configured model the goal "fix it".
3. It runs the change through the normal flow (review, isolated worktree, tests in the sandbox).
4. It reports whether the tests passed.
5. It deletes the throwaway project and its Coding Brain data.

Your projects are never involved.

## Updates, rollback and uninstall

`codingbrain update`, `codingbrain rollback` and `uninstall.ps1` change only Coding Brain's own
application folders. They leave alone:

- WSL and Docker Desktop, and Docker's images;
- Ollama and its models;
- Claude Code and Codex;
- Git and Python.

Your configuration, project memory and installer checkpoints are kept across updates.

## Security

- Every change is asked first. Administrator rights are requested only for steps that need them
  (WSL, Docker Desktop, Tesseract), always through Windows' own UAC prompt.
- Sources are winget (which verifies installer hashes from its manifests) or the vendor's official
  channel. No remote script is executed. No security warning is suppressed. No third-party binary
  is bundled.
- After a winget install, the program's Authenticode signature is recorded. A `HashMismatch` or
  `NotTrusted` binary is refused.
- Logs, the activity journal and the checkpoint file are redacted. Proxy settings are recorded by
  name only. No passwords, tokens or API keys are stored.

## What the automated tests prove, and what they do not

- **Simulated** (`tests/test_installer.py`, using `tests/installer_fakes.py`): the installer's
  decisions on a simulated Windows computer. These cover clean machines, a restart and resume,
  idempotence, interruption, declines, missing winget, virtualization signals, UAC refusal,
  Docker engine states, a model that cannot generate, memory sizing, low disk, all sign-in states,
  offline machines, tampered binaries, locking, secrets and progress. They do **not** prove that
  real virtualization, WSL, Docker Desktop, winget installs or subscription sign-ins work.
- **Real, Windows CI runners:**
  - the preflight probes on a real Windows VM;
  - `winget show` for every package ID (where winget exists on the image);
  - `install.ps1 -Full -PlanOnly` handing over to provisioning before the CLI is on PATH;
  - `doctor --json` readiness.

  GitHub's Windows runners have no nested virtualization, WSL 2 or Linux containers, so there
  these checks show only the not-ready path.
- **Real, Linux CI runner:**
  - `codingbrain install --profile local` with a real Ollama, downloading and generating with
    `qwen2.5-coder:7b`;
  - `doctor --full` with real sandbox images;
  - the real end-to-end self-test.
- **Not tested automatically anywhere:**
  - a real WSL 2 installation and restart;
  - Docker Desktop's installation and first start;
  - Claude/Codex sign-ins with real subscriptions;
  - GPU sizing on real hardware.

  These need a real Windows PC (see the release-readiness report).
