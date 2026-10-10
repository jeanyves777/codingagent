"""Codex detection in the desktop (fallback for engines without the typed API) and the installed
engine's providers.list winning when it is available. Hermetic: simulated Windows folders and
registry; nothing is executed to find a CLI and nothing signs in."""
import pytest

from desktop_ui import management


@pytest.fixture
def windows(tmp_path, monkeypatch):
    home, appdata, local, empty = (tmp_path / name for name in ("home", "Roaming", "Local", "empty"))
    for folder in (home, appdata, local, empty):
        folder.mkdir()
    monkeypatch.setattr(management, "IS_WINDOWS", True)
    monkeypatch.setenv("PATH", str(empty))  # the desktop's inherited PATH
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
    registry = []
    monkeypatch.setattr(management, "_registry_path_dirs", lambda: list(registry))
    ran = []
    monkeypatch.setattr(management.subprocess, "run", lambda *a, **k: ran.append(a) or pytest.fail("nothing may run"))
    return {"home": home, "appdata": appdata, "registry": registry, "tmp": tmp_path, "ran": ran}


def place(folder, name="codex.cmd"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("@echo off\n")
    return str(folder / name)


def codex_row(report):
    return next(item for item in report["components"] if item["id"] == "codex")


def test_a_default_roaming_npm(windows):
    expected = place(windows["appdata"] / "npm")
    row = codex_row(management.installation_probes(None))
    assert row["installed"] and row["path"] == expected and expected in row["detail"]


def test_b_nondefault_npm_prefix(windows):
    prefix = windows["tmp"] / "D-drive" / "npm-global"
    (windows["home"] / ".npmrc").write_text(f"prefix={prefix}\n")
    expected = place(prefix)
    assert codex_row(management.installation_probes(None))["path"] == expected


def test_c_visible_to_powershell_but_not_the_inherited_ui_path(windows):
    folder = windows["tmp"] / "Programs" / "codex"
    windows["registry"].append(str(folder))  # user PATH updated after the desktop started
    expected = place(folder, "codex.exe")
    row = codex_row(management.installation_probes(None))
    assert row["installed"] and row["path"] == expected  # no reboot or restart needed


def test_d_truly_missing_says_where_it_looked(windows):
    row = codex_row(management.installation_probes(None))
    assert not row["installed"] and row["checked"][0] == "PATH"
    assert str(windows["appdata"] / "npm" / "codex.cmd") in row["checked"]
    assert "Recheck" in row["detail"] and "Missing from PATH" not in row["detail"]


def test_e_the_installed_engine_wins_over_the_desktop_lookup(windows):
    place(windows["appdata"] / "npm")  # the desktop would find a launcher here
    engine = [{"id": "codex", "installed": False, "cli_path": None,
               "checked": ["PATH", r"C:\Users\me\AppData\Roaming\npm\codex.cmd"]},
              {"id": "claude", "installed": True, "cli_path": r"C:\Users\me\.local\bin\claude.exe", "checked": []}]
    report = management.installation_probes(None, engine)
    codex, claude = codex_row(report), next(i for i in report["components"] if i["id"] == "claude")
    assert codex["installed"] is False and codex["source"] == "engine"
    assert claude["installed"] and claude["path"].endswith("claude.exe") and claude["source"] == "engine"
    assert windows["ran"] == []  # detection never runs a CLI, signs in or spends premium budget


def test_standalone_codex_for_windows_without_npm(windows, monkeypatch):
    """The owner's PC: %LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin\\codex.exe, npm not installed."""
    import os
    from pathlib import Path
    expected = place(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "OpenAI" / "Codex" / "bin", "codex.exe")
    row = codex_row(management.installation_probes(None))
    assert row["installed"] and row["path"] == expected
