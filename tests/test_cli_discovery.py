"""Finding vendor CLIs (Codex, Claude) on Windows without running them, when the process's PATH is
stale or npm uses a non-default prefix. Hermetic: the Windows paths are simulated with temporary
folders and a fake registry; nothing is executed to discover a command."""
import pytest

from brain.local import system as system_module
from brain.local.system import System


@pytest.fixture
def windows(tmp_path, monkeypatch):
    home, appdata, local = tmp_path / "home", tmp_path / "Roaming", tmp_path / "Local"
    for folder in (home, appdata, local):
        folder.mkdir()
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setattr(system_module, "WINDOWS", True)
    monkeypatch.setenv("PATH", str(empty))  # the inherited PATH: no CLIs on it
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
    registry = []
    monkeypatch.setattr(System, "_registry_path_dirs", lambda self: list(registry))
    return {"home": home, "appdata": appdata, "local": local, "registry": registry, "tmp": tmp_path}


def place(folder, name="codex.cmd"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("@echo off\n")
    return str(folder / name)


def test_default_roaming_npm(windows):
    expected = place(windows["appdata"] / "npm")
    assert System().locate("codex")[0] == expected


def test_nondefault_npm_prefix_from_npmrc(windows):
    prefix = windows["tmp"] / "tools" / "npm-global"
    (windows["home"] / ".npmrc").write_text(f"registry=https://registry.npmjs.org/\nprefix={prefix}\n")
    expected = place(prefix)
    assert System().locate("codex")[0] == expected


def test_nondefault_npm_prefix_from_environment(windows, monkeypatch):
    prefix = windows["tmp"] / "npm-env"
    monkeypatch.setenv("NPM_CONFIG_PREFIX", str(prefix))
    expected = place(prefix)
    assert System().locate("codex")[0] == expected


def test_on_the_registry_path_but_not_the_inherited_path(windows):
    folder = windows["tmp"] / "Programs" / "codex-bin"
    windows["registry"].append(str(folder))  # added by an installer after this process started
    expected = place(folder, "codex.exe")
    assert System().locate("codex")[0] == expected


def test_truly_missing_reports_every_place_checked(windows):
    path, checked = System().locate("codex")
    assert path is None and checked[0] == "PATH"
    assert str(windows["appdata"] / "npm" / "codex.cmd") in checked
    assert any("WinGet" in item for item in checked)


def test_missing_codex_status_names_the_places_checked(windows, monkeypatch):
    from brain.local.components import Codex, Env
    from brain.local.paths import Layout
    from brain.local import config as settings
    layout = Layout(windows["tmp"] / "cb").ensure()
    env = Env(layout=layout, config=settings.load(layout), system=System())
    ran = []
    monkeypatch.setattr(System, "run", lambda self, arguments, **kw: ran.append(arguments) or (0, ""))
    status = Codex().check(env)
    assert status.state == "missing" and "looked in" in status.detail and ran == []  # nothing executed
    assert status.data["checked"]


def test_found_off_path_codex_is_run_by_its_full_path(windows, monkeypatch):
    from brain.local.components import Codex, Env
    from brain.local.paths import Layout
    from brain.local import config as settings
    launcher = place(windows["appdata"] / "npm")
    layout = Layout(windows["tmp"] / "cb").ensure()
    env = Env(layout=layout, config=settings.load(layout), system=System())
    ran = []
    monkeypatch.setattr(System, "run", lambda self, arguments, **kw: ran.append(arguments) or (0, "Logged in using ChatGPT"))
    status = Codex().check(env)
    assert ran and all(arguments[0] == launcher for arguments in ran)  # not the bare name, which is not on PATH
    assert status.data["path"] == launcher


@pytest.mark.parametrize("enabled,readiness", [(False, "disabled"), (True, "ready")])
def test_standalone_codex_for_windows_without_npm_or_path(windows, monkeypatch, enabled, readiness):
    """The owner's machine: the standalone Codex at %LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin, not
    on the desktop's PATH, no npm at all, signed in with ChatGPT, no API key. Found, authenticated,
    and usable only when enabled as a supervisor; nothing to reinstall."""
    from brain.local import config as settings
    from brain.local import providers
    from brain.local.components import Codex, Env, Status
    from brain.local.paths import Layout
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    launcher = place(windows["local"] / "Programs" / "OpenAI" / "Codex" / "bin", "codex.exe")
    replies = {"--version": "codex-cli 0.162.1", "status": "Logged in using ChatGPT"}
    ran = []
    monkeypatch.setattr(System, "run", lambda self, arguments, **kw: ran.append(arguments) or (0, replies[arguments[-1]]))
    layout = Layout(windows["tmp"] / "cb").ensure()
    config = settings.load(layout)
    config["supervisors"].setdefault("codex", {})["enabled"] = enabled
    env = Env(layout=layout, config=config, system=System())
    assert System().locate("codex")[0] == launcher
    monkeypatch.setattr(providers, "check_all", lambda env, ids, deep=False: {
        "ollama": Status("missing", "not here"), "model": Status("missing", "not here"),
        "claude": Status("missing", "claude CLI not found", data={"auth": "not_installed"}),
        "codex": Codex().check(env)})
    codex = next(item for item in providers.descriptors(env) if item["id"] == "codex")
    assert codex["installed"] and codex["cli_path"] == launcher
    assert codex["authentication"] == "signed_in" and codex["enabled"] is enabled
    assert codex["readiness"] == readiness and codex["billing_type"] == "subscription"
    assert all(arguments[0] == launcher for arguments in ran)  # the found launcher, not a PATH lookup
    assert not any("npm" in " ".join(arguments) for arguments in ran)
