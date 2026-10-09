"""Installer, dependency management and readiness levels.

Most scenarios run against tests/installer_fakes.FakeMachine, a SIMULATED Windows computer: they
prove the installer's decisions, checkpoints, consent, progress and reporting, not that real
virtualization, WSL, Docker, winget or subscription sign-ins work. Tests named test_real_* use the
real machine and are skipped unless what they need is present (see the workflow for where they run).
"""
import io
import json
import os
import sys
from pathlib import Path

import pytest

from brain.local import config as settings
from brain.local.components import (BY_ID, CODING_MODELS, GB, VISION_MODELS, Env, Status, check_all,
                                    classify_auth, recommend)
from brain.local.installer import InstallState, Installer, preflight
from brain.local.paths import Layout
from brain.local.readiness import assess, render
from brain.local.system import assess_virtualization

from installer_fakes import FakeMachine

SIMULATED = "simulated machine"


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setenv("CODINGBRAIN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("brain.accounting.HEARTBEAT_SECONDS", 0.2, raising=False)
    return Layout(tmp_path / "home").ensure()


def make(layout, machine, answers=None, yes=False):
    """An installer on a simulated machine. `answers` maps a question fragment to the reply;
    unmatched questions get their default."""
    asked = []

    def ask(question, default=False):
        asked.append(question)
        for fragment, reply in (answers or {}).items():
            if fragment.lower() in question.lower():
                return reply
        return True if yes else default
    out = io.StringIO()
    installer = Installer(layout, system=machine, ask=ask, mode="plain", stream=out, interactive=True)
    installer.asked, installer.out = asked, out
    return installer


def installs(machine) -> list[str]:
    return [call[call.index("--id") + 1] for call in machine.calls if call[0] == "winget" and "install" in call]


def journal(layout, run=None) -> list[dict]:
    from brain.telemetry import Telemetry
    return Telemetry(layout.data / "install.sqlite3").journal(run and [run], limit=10000)


# 1-4: clean machines, resume after restart, idempotence, interruption --------------------------------

def test_clean_machine_local_profile_reaches_core_ready(layout):
    machine = FakeMachine()
    installer = make(layout, machine, yes=True)
    report = installer.install("local")
    assert installs(machine) == ["Git.Git", "Ollama.Ollama"]
    assert "qwen2.5-coder:7b" in machine.models  # 16 GB of memory
    assert report["level"] == "core", render(report)
    assert report["levels"]["core"]["ready"] and not report["levels"]["sandbox"]["ready"]
    assert settings.load(layout)["models"]["model"] == "qwen2.5-coder:7b"
    state = json.loads((layout.home / "install" / "state.json").read_text())
    assert {state["steps"][key]["state"] for key in ("git", "ollama", "model")} == {"done"}
    evidence = json.loads((layout.home / "install" / "evidence.json").read_text())
    assert evidence["generation"]["qwen2.5-coder:7b"]["ok"]  # the model generated text, not just downloaded


def test_full_profile_restarts_for_wsl_then_resumes_to_full_ready(layout):
    machine = FakeMachine()
    first = make(layout, machine, yes=True).install("full")
    assert first["restart_required"] == "wsl"
    assert machine.resume_command and "install --resume" in machine.resume_command
    assert "Docker.DockerDesktop" not in installs(machine)  # nothing after the restart point yet
    assert ["elevated", "wsl.exe", "--install", "--no-distribution"] in machine.calls
    machine.restart()
    second = make(layout, machine, yes=True).install(resume=True)
    assert machine.resume_command is None
    assert installs(machine).count("Git.Git") == 1  # done steps are not repeated
    assert "Docker.DockerDesktop" in installs(machine)
    assert ["interactive", "claude", "auth", "login"] in machine.calls
    assert ["interactive", "codex", "login"] in machine.calls
    # subscriptions are signed in but stay off until you turn them on
    assert second["level"] == "sandbox" and "claude or codex" in " ".join(second["levels"]["hybrid"]["missing"])
    config = settings.load(layout)
    config["supervisors"]["claude"]["enabled"] = True
    settings.save(layout, config)
    third = make(layout, machine, yes=True).install(resume=True)
    assert third["level"] == "full", render(third)


def test_rerun_is_idempotent(layout):
    machine = FakeMachine()
    make(layout, machine, yes=True).install("local")
    before = len(installs(machine))
    report = make(layout, machine, yes=True).install("local")
    assert len(installs(machine)) == before
    assert report["level"] == "core"


def test_interrupted_download_is_checked_again_not_assumed(layout):
    machine = FakeMachine(programs={"winget", "git"}, ollama_running=True)
    machine.interrupt_on = lambda program, rest: False
    installer = make(layout, machine, yes=True)
    original = machine.http

    def broken(method, url, payload=None, timeout=10, on_json_line=None):
        if url.endswith("/api/pull"):
            raise KeyboardInterrupt
        return original(method, url, payload, timeout, on_json_line)
    machine.http = broken
    with pytest.raises(KeyboardInterrupt):
        installer.install("local")
    state = json.loads((layout.home / "install" / "state.json").read_text())
    assert state["steps"]["model"]["state"] == "interrupted"
    machine.http = original
    report = make(layout, machine, yes=True).install(resume=True)
    assert report["level"] == "core"
    assert not (layout.locks / "install.json").exists()


# 5-6: consent and missing winget ----------------------------------------------------------------------

def test_declined_component_is_remembered_and_not_degraded(layout):
    machine = FakeMachine(wsl="installed")
    answers = {"Docker": False, "Claude": False, "Codex": False, "knowledge": False, "Tesseract": False, "vision": False}
    report = make(layout, machine, answers).install("full")
    assert "Docker.DockerDesktop" not in installs(machine)
    assert report["level"] == "core"  # a choice, not a fault
    second = make(layout, machine, answers)
    second.install("full")
    assert not any("Docker" in question for question in second.asked)  # not asked again
    third = make(layout, machine, {"Docker": True})
    third.install("full", only=["docker"])
    assert any("Docker" in question for question in third.asked)


def test_without_winget_gives_instructions_and_blocks_honestly(layout):
    machine = FakeMachine(programs={"powershell"})
    report = make(layout, machine, yes=True).install("local")
    assert report["level"] == "blocked"
    assert report["components"]["git"]["state"] == "unsupported"
    assert "App Installer" in report["components"]["git"]["detail"]
    assert installs(machine) == []


# 7-9: virtualization and WSL ---------------------------------------------------------------------------

def test_hypervisor_running_means_virtualization_enabled_despite_firmware_flag():
    result = assess_virtualization({"firmware_virtualization": False, "hypervisor_present": True})
    assert result["state"] == "enabled"


def test_firmware_flag_alone_is_never_reported_as_definitely_disabled():
    result = assess_virtualization({"firmware_virtualization": False, "hypervisor_present": False})
    assert result["state"] == "possibly_disabled"
    assert "can be wrong" in result["advice"] and "never changes firmware" in result["advice"]
    assert "definitely" not in json.dumps(result).lower()
    assert assess_virtualization({})["state"] == "unknown"
    assert assess_virtualization({"firmware_virtualization": False}, docker_linux=True)["state"] == "enabled"


def test_uac_declined_for_wsl_leaves_docker_waiting(layout):
    machine = FakeMachine(programs={"winget", "git"}, uac_declined=True)
    report = make(layout, machine, yes=True).install("full", only=["wsl", "docker"])
    assert report["components"]["wsl"]["state"] == "failed"
    assert "administrator approval" in report["components"]["wsl"]["detail"]
    assert report["components"]["docker"]["state"] == "waiting"
    assert "Docker.DockerDesktop" not in installs(machine)


# 10-11: Docker CLI versus engine --------------------------------------------------------------------------

def test_installed_docker_is_started_and_waited_for(layout):
    machine = FakeMachine(programs={"winget", "git", "docker", "docker_desktop"}, wsl="installed")
    report = make(layout, machine, yes=True).install("full", only=["docker"])
    assert any(call[0] == "start" and "Docker Desktop.exe" in call[1] for call in machine.calls)
    assert report["components"]["docker"]["state"] == "ready"


def test_docker_engine_that_never_starts_is_degraded_with_instructions(layout):
    machine = FakeMachine(programs={"winget", "git", "docker", "docker_desktop", "ollama"}, wsl="installed",
                          docker_starts=False, ollama_running=True, models={"qwen2.5-coder:7b"})
    report = make(layout, machine, yes=True).install("full", only=["docker"])
    docker = report["components"]["docker"]
    assert docker["state"] == "stopped" and "accept its terms" in docker["detail"]
    assert report["level"] == "degraded" and report["reached"] == "core"


def test_windows_containers_mode_needs_the_user(layout):
    machine = FakeMachine(programs={"winget", "docker", "docker_desktop"}, wsl="installed", docker_engine="windows")
    env = Env(machine, layout, settings.load(layout))
    status = BY_ID["docker"].check(env)
    assert status.state == "failed" and status.action == "manual" and "Linux containers" in status.detail


# 12-14: models: proof of execution, sizing, disk -------------------------------------------------------------

def test_downloaded_model_that_cannot_generate_blocks_core(layout):
    machine = FakeMachine(programs={"winget", "git", "ollama"}, ollama_running=True, models={"qwen2.5-coder:7b"},
                          generation_ok=False)
    report = make(layout, machine, yes=True).install("local")
    assert report["components"]["model"]["state"] == "failed"
    assert "generation failed" in report["components"]["model"]["detail"]
    assert report["level"] == "blocked"


def test_quick_check_does_not_count_an_untested_model():
    results = {"python": Status("ready"), "git": Status("ready"), "ollama": Status("ready"),
               "model": Status("ready", "downloaded", data={"unverified": True})}
    report = assess(results)
    assert not report["levels"]["core"]["ready"] and report["unverified"] == ["model"]
    assert "doctor --full" in render(report)


def test_model_recommendation_follows_memory():
    assert recommend(CODING_MODELS, 4 * GB)[0] == "qwen2.5-coder:1.5b"
    assert recommend(CODING_MODELS, 8 * GB)[0] == "qwen2.5-coder:3b"
    assert recommend(CODING_MODELS, 16 * GB)[0] == "qwen2.5-coder:7b"
    assert recommend(CODING_MODELS, 32 * GB)[0] == "qwen2.5-coder:14b"
    assert recommend(CODING_MODELS, 16 * GB, vram=24 * GB)[0] == "qwen2.5-coder:14b"
    assert recommend(VISION_MODELS, 8 * GB)[0] == "qwen2.5vl:3b"


def test_low_disk_space_refuses_the_download_and_blocks_preflight(layout):
    machine = FakeMachine(programs={"winget", "git", "ollama"}, ollama_running=True, disk=5 * GB)
    report = make(layout, machine, yes=True).install("local")
    assert "not enough disk space" in report["components"]["model"]["detail"]
    assert not machine.models
    machine.disk = 2 * GB
    assert preflight(machine, layout, "local")["blockers"]


# 15-19: Claude Code and Codex --------------------------------------------------------------------------------

def test_sign_in_uses_official_flow_without_api_keys(layout):
    machine = FakeMachine(programs={"winget", "npm", "node"})
    machine.environment.update({"ANTHROPIC_API_KEY": "sk-ant-secret-value", "OPENAI_API_KEY": "sk-proj-secret-value"})
    report = make(layout, machine, yes=True).install("full", only=["claude", "codex"])
    assert "Anthropic.ClaudeCode" in installs(machine)
    assert ["npm", "install", "--global", "@openai/codex"] in machine.calls
    claude_env, codex_env = machine.interactive_env
    assert "ANTHROPIC_API_KEY" not in claude_env and "OPENAI_API_KEY" not in codex_env
    assert report["components"]["claude"]["data"]["auth"] in {"authenticated", "disabled"}
    stored = b"".join(path.read_bytes() for path in layout.home.rglob("*") if path.is_file())
    assert b"sk-ant-secret-value" not in stored and b"sk-proj-secret-value" not in stored


@pytest.mark.parametrize("name,state,expected", [
    ("claude", "subscription", "authenticated"), ("claude", "api_key", "api_key_billing"),
    ("claude", "expired", "expired"), ("claude", "none", "not_authenticated"),
    ("claude", "offline", "temporarily_unavailable"), ("codex", "subscription", "authenticated"),
    ("codex", "api_key", "api_key_billing"), ("codex", "expired", "expired"), ("codex", "none", "not_authenticated"),
    ("codex", "offline", "temporarily_unavailable")])
def test_auth_states(name, state, expected):
    machine = FakeMachine()
    machine.auth[name] = state
    code, text = machine.auth_status(name, {})
    assert classify_auth(name, code, text)[0] == expected


def test_api_key_billing_never_counts_as_hybrid(layout):
    machine = FakeMachine(programs={"claude"}, auth={"claude": "api_key", "codex": "none"})
    config = settings.load(layout)
    config["supervisors"]["claude"]["enabled"] = True
    env = Env(machine, layout, config)
    status = BY_ID["claude"].check(env)
    assert status.state == "needs_sign_in" and status.data["auth"] == "api_key_billing"
    assert "billed separately" in status.detail


def test_expired_sign_in_of_an_enabled_supervisor_is_degraded(layout):
    results = {name: Status("ready") for name in ("python", "git", "ollama", "model", "docker", "sandbox")}
    results["codex"] = Status("needs_sign_in", "expired", data={"auth": "expired"})
    config = settings.load(layout)
    config["supervisors"]["codex"]["enabled"] = True
    report = assess(results, config=config)
    assert report["level"] == "degraded" and report["reached"] == "sandbox"
    assert report["degraded"][0]["component"] == "codex"


def test_temporarily_unavailable_does_not_start_a_sign_in(layout):
    machine = FakeMachine(programs={"winget", "claude"}, auth={"claude": "offline", "codex": "none"})
    report = make(layout, machine, yes=True).install("full", only=["claude"])
    assert report["components"]["claude"]["state"] == "unavailable"
    assert not any(call[0] == "interactive" for call in machine.calls)


def test_signed_in_but_turned_off_is_reported_as_disabled(layout):
    machine = FakeMachine(programs={"claude"}, auth={"claude": "subscription", "codex": "none"})
    status = BY_ID["claude"].check(Env(machine, layout, settings.load(layout)))
    assert status.ready and status.data["auth"] == "disabled"
    from brain.local.readiness import premium_ready
    assert premium_ready({"claude": status}) == []


# 20-24: network, integrity, locking, secrets, live progress -----------------------------------------------------

def test_offline_machine_fails_downloads_gracefully(layout):
    machine = FakeMachine(network=False, programs={"winget", "git", "ollama"}, ollama_running=True)
    report = make(layout, machine, yes=True).install("local")
    assert any("no network access" in warning for warning in report["preflight"]["warnings"])
    assert report["components"]["git"]["state"] == "ready"  # what is present still works
    assert report["components"]["model"]["state"] == "failed" and "no such host" in report["components"]["model"]["detail"]
    assert report["level"] == "blocked"


def test_tampered_binary_is_refused(layout):
    machine = FakeMachine(tampered={"ollama"}, programs={"winget", "git"})
    report = make(layout, machine, yes=True).install("local", only=["ollama"])
    assert report["components"]["ollama"]["state"] in {"failed", "ready"}
    state = json.loads((layout.home / "install" / "state.json").read_text())
    assert state["steps"]["ollama"]["state"] == "failed" and "HashMismatch" in state["steps"]["ollama"]["detail"]


def test_one_installation_at_a_time(layout):
    import subprocess
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (layout.locks / "install.json").write_text(json.dumps({"pid": sleeper.pid, "at": __import__("time").time()}))
        with pytest.raises(SystemExit, match="Another Coding Brain installation"):
            make(layout, FakeMachine(), yes=True).install("local")
    finally:
        sleeper.kill()


def test_proxy_values_and_tokens_never_reach_logs(layout):
    machine = FakeMachine()
    machine.environment.update({"HTTPS_PROXY": "http://user:proxy-password@proxy:8080",
                                "GITHUB_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"})
    installer = make(layout, machine, yes=True)
    report = installer.install("local")
    assert report["preflight"]["proxy"] == ["HTTPS_PROXY"]
    everything = b"".join(path.read_bytes() for path in layout.home.rglob("*") if path.is_file())
    everything += installer.out.getvalue().encode()
    assert b"proxy-password" not in everything and b"ghp_abcdefghijklmnopqrstuvwxyz" not in everything


def test_progress_is_journaled_from_real_tool_output(layout):
    machine = FakeMachine()
    installer = make(layout, machine, yes=True)
    installer.install("local")
    events = journal(layout, installer.run_id)
    kinds = {(event["event_type"], event["status"]) for event in events}
    assert ("operation", "RUNNING") in kinds and ("operation", "COMPLETED") in kinds
    progress = [event["summary"] for event in events if event["event_type"] == "progress"]
    assert any("GB" in line and " of " in line for line in progress)  # bytes reported by Ollama
    assert all("%" not in line or "ollama" in line or "winget" in line.lower() for line in progress)
    assert all(event["agent"] == "installer" for event in events)
    text = installer.out.getvalue()
    assert "Installer — " in text and "Readiness:" in text


# 25-27: doctor, updates, uninstall ------------------------------------------------------------------------------

def test_doctor_reports_levels_instead_of_healthy(layout, monkeypatch, capsys):
    from brain.local import cli
    machine = FakeMachine(programs={"winget", "git", "ollama", "docker", "docker_desktop"}, wsl="installed",
                          ollama_running=True, models={"qwen2.5-coder:7b"}, docker_engine=None)
    monkeypatch.setattr("brain.local.system.System", lambda: machine)
    config = settings.load(layout)
    config["models"]["model"] = "qwen2.5-coder:7b"
    settings.save(layout, config)
    code = cli.main(["doctor", "--full"])
    out = capsys.readouterr().out
    assert "Healthy" not in out
    assert "Readiness: Degraded (reached Core ready)" in out  # Docker installed but its engine is not running
    assert code == 0  # the application itself is fine
    cli.main(["doctor", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["readiness"]["level"] == "degraded" and report["readiness"]["levels"]["core"]["ready"]


def test_updates_rollback_and_uninstall_never_touch_shared_dependencies():
    root = Path(__file__).resolve().parents[1]
    forbidden = ["winget uninstall", "wsl --unregister", "wsl --uninstall", "docker rmi", "docker volume rm",
                 "docker system prune", "ollama rm", "npm uninstall", "Remove-AppxPackage"]
    for path in [root / "brain/local/updater.py", root / "brain/local/assets/uninstall.ps1",
                 root / "brain/local/installer.py", root / "brain/local/components.py", root / "brain/local/system.py",
                 root / "installer/windows/install.ps1"]:
        text = path.read_text(encoding="utf-8")
        for command in forbidden:
            assert command not in text, f"{path.name} contains {command}"
    assert "were not changed" in (root / "brain/local/assets/uninstall.ps1").read_text(encoding="utf-8")


def test_installer_never_bypasses_security_or_runs_remote_scripts():
    root = Path(__file__).resolve().parents[1]
    text = "\n".join((root / name).read_text(encoding="utf-8") for name in (
        "brain/local/components.py", "brain/local/system.py", "brain/local/installer.py", "installer/windows/install.ps1"))
    for pattern in ("Invoke-Expression", "| iex", "irm ", "curl | sh", "| sh", "--ignore-security-hash", "--force",
                    "Set-ExecutionPolicy", "bcdedit", "Set-MpPreference", "DisableRealtimeMonitoring"):
        assert pattern not in text, pattern
    assert "Verb RunAs" in text  # elevation goes through the UAC prompt


def test_checkpoint_survives_a_new_process(layout):
    machine = FakeMachine()
    make(layout, machine, {"Docker": False}, yes=False).install("full", only=["docker"])
    state = InstallState(layout)
    assert state.data["profile"] == "full" and "docker" in state.data["declined"]


# Real machine -------------------------------------------------------------------------------------------------------

def test_real_preflight_on_this_machine(layout):
    """Real probes (memory, disk, administrator, winget, virtualization signals) on the machine
    running the tests: on the Windows CI runners this is a real Windows VM."""
    from brain.local.system import System
    system = System()
    facts = preflight(system, layout, "full")
    assert not facts["simulated"]
    assert facts["disk_free_gb"] > 0
    assert facts["virtualization"]["state"] in {"enabled", "possibly_disabled", "unknown"}
    if sys.platform == "win32":
        assert facts["build"] and facts["winget"] is not None and facts["ram_gb"]
    print(json.dumps(facts, indent=2, default=str))


@pytest.mark.skipif(not os.environ.get("CODINGBRAIN_TEST_WINGET"), reason="set CODINGBRAIN_TEST_WINGET=1 (Windows with winget)")
def test_real_winget_package_ids_exist():
    """Every winget package the installer names is in the official winget source (real query)."""
    from brain.local.components import COMPONENTS, NodeJS
    from brain.local.system import System
    system = System()
    ids = {component.winget_id for component in COMPONENTS if component.winget_id} | {NodeJS.winget_id,
                                                                                      "Python.Python.3.12"}
    missing = []
    for package in sorted(ids):
        code, output = system.run(["winget", "show", "--exact", "--id", package, "--source", "winget",
                                   "--accept-source-agreements", "--disable-interactivity"], timeout=300)
        if code != 0:
            missing.append(f"{package}: {output.strip()[-200:]}")
    assert not missing, missing


@pytest.mark.skipif(not os.environ.get("CODINGBRAIN_TEST_CODING_MODEL"), reason="needs a real Ollama model")
def test_real_model_generation_and_core_readiness(layout):
    """A real local model, through the real Ollama server: the readiness level comes from text it generated."""
    from brain.local.system import System
    config = settings.load(layout)
    config["models"]["model"] = os.environ["CODINGBRAIN_TEST_CODING_MODEL"]
    settings.save(layout, config)
    env = Env(System(), layout, config, evidence={})
    results = check_all(env, ["python", "git", "ollama", "model"], deep=True)
    assert results["model"].ready and results["model"].verified, results["model"].detail
    assert "tokens in" in results["model"].detail
    assert assess(results)["levels"]["core"]["ready"]


@pytest.mark.skipif(not (os.environ.get("CODINGBRAIN_TEST_CODING_MODEL") and os.environ.get("CODINGBRAIN_TEST_DOCKER")),
                    reason="needs a real Ollama model and Docker with the sandbox images")
def test_real_end_to_end_selftest(layout):
    """The disposable end-to-end task with the real model and the real sandbox."""
    from brain.local.installer import selftest
    config = settings.load(layout)
    config["models"]["model"] = os.environ["CODINGBRAIN_TEST_CODING_MODEL"]
    settings.save(layout, config)
    result = selftest(layout)
    print(json.dumps(result, indent=2))
    assert result["passed"], result
    assert not Path(result["project"]).exists()  # the throwaway project is removed


def test_no_sign_in_flow_without_a_terminal(layout):
    machine = FakeMachine(programs={"winget", "claude"})
    installer = make(layout, machine, yes=True)
    installer.interactive = False
    report = installer.install("full", only=["claude"])
    assert not any(call[0] == "interactive" for call in machine.calls)
    state = json.loads((layout.home / "install" / "state.json").read_text())
    assert "sign in from a terminal" in state["steps"]["claude"]["detail"]
    assert report["components"]["claude"]["state"] == "needs_sign_in"
