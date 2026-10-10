"""The official release and desktop updater must be version-pinned and honestly report capabilities."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from desktop_ui import updates
from scripts.finalize_desktop_release import finalize


def _backend_release(tmp_path, version="0.13.0"):
    root = tmp_path / "release"
    root.mkdir()
    (root / f"coding_brain-{version}-py3-none-any.whl").write_bytes(b"fake wheel")
    (root / "release.json").write_text(
        json.dumps({"version": version, "tag": "v" + version, "channel": "stable"}), encoding="utf-8")
    (root / "SHA256SUMS").write_text("old manifest\n", encoding="utf-8")
    return root


def test_release_contains_matching_desktop_version_and_checksums(tmp_path):
    root = _backend_release(tmp_path)
    installer = tmp_path / "setup.exe"
    portable = tmp_path / "portable.zip"
    installer.write_bytes(b"e" * 2048)
    portable.write_bytes(b"z" * 2048)
    info = finalize(root, installer, portable)
    assert info["desktop"] == {"version": "0.13.0", "installer": "CodingBrain-Setup.exe",
                                "portable": "CodingBrainDesktop-Windows.zip"}
    assert info["version"] == info["desktop"]["version"]
    entries = {}
    for line in (root / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(None, 1)
        assert digest == hashlib.sha256((root / name).read_bytes()).hexdigest()
        entries[name] = digest
    assert {"CodingBrain-Setup.exe", "CodingBrainDesktop-Windows.zip", "release.json",
            "coding_brain-0.13.0-py3-none-any.whl"} <= set(entries)
    with pytest.raises(ValueError, match="already finalized"):
        finalize(root, installer, portable)


def _verify_asset(folder: Path, filename: str) -> subprocess.CompletedProcess:
    """Run the updater's own Verify-Asset (and Expected-Sha256) from Update-CodingBrain.ps1."""
    script = str(Path(__file__).resolve().parents[1] / "desktop_ui" / "Update-CodingBrain.ps1").replace("'", "''")
    command = (f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$null,[ref]$null);"
               "foreach($f in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
               "$n.Name -in @('Expected-Sha256','Verify-Asset')}, $true)) { . ([scriptblock]::Create($f.Extent.Text)) };"
               f"Verify-Asset '{filename}' '{str(folder).replace(chr(39), chr(39) * 2)}' "
               f"'{str(folder / 'SHA256SUMS').replace(chr(39), chr(39) * 2)}'")
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(os.name != "nt", reason="runs the updater's PowerShell checksum check")
def test_updater_refuses_a_tampered_installer(tmp_path):
    root = _backend_release(tmp_path)
    installer, portable = tmp_path / "setup.exe", tmp_path / "portable.zip"
    installer.write_bytes(b"e" * 2048)
    portable.write_bytes(b"z" * 2048)
    finalize(root, installer, portable)
    accepted = _verify_asset(root, "CodingBrain-Setup.exe")
    assert accepted.returncode == 0, accepted.stderr
    (root / "CodingBrain-Setup.exe").write_bytes(b"tampered")
    refused = _verify_asset(root, "CodingBrain-Setup.exe")
    assert refused.returncode != 0 and "SHA256 mismatch" in refused.stdout + refused.stderr


def test_desktop_installer_never_accepts_the_environment_plan_for_the_user():
    """The installer's setup step must show Coding Brain's plan and ask: -Yes/--yes only when the
    setup script itself was started unattended with -Yes, never by default."""
    root = Path(__file__).resolve().parents[1] / "installer" / "windows"
    iss = (root / "CodingBrainDesktop.iss").read_text(encoding="utf-8")
    setup_line = next(line for line in iss.splitlines() if "Setup-CodingBrain.ps1" in line and line.startswith("Filename"))
    assert not re.search(r"(?i)\s-Yes\b", setup_line)
    script = (root / "Setup-CodingBrain.ps1").read_text(encoding="utf-8")
    assert not re.search(r"(?i)-Full -Yes|install --full --yes", script)
    assert "$installYes = if ($Yes)" in script and "$cliYes = if ($Yes)" in script


@pytest.mark.parametrize("problem", ["wrong_tag", "missing_exe", "tiny_exe"])
def test_packaging_fails_closed_before_claiming_desktop_release(tmp_path, problem):
    root = _backend_release(tmp_path)
    exe, archive = tmp_path / "desktop.exe", tmp_path / "desktop.zip"
    exe.write_bytes(b"exe" * 500)
    archive.write_bytes(b"zip" * 500)
    if problem == "wrong_tag":
        (root / "release.json").write_text('{"version":"0.13.0","tag":"v0.12.0"}')
    elif problem == "missing_exe":
        exe.unlink()
    else:
        exe.write_bytes(b"x")
    with pytest.raises(ValueError):
        finalize(root, exe, archive)
    assert "desktop" not in json.loads((root / "release.json").read_text())


def _fake_github(data):
    return io.BytesIO(json.dumps(data).encode("utf-8"))


def test_legacy_engine_only_release_is_never_reported_as_desktop_upgrade(monkeypatch, tmp_path):
    version = tmp_path / "desktop-version.json"
    version.write_text('{"version":"0.10.0"}')
    monkeypatch.setattr(updates, "VERSION_FILE", version)
    monkeypatch.setattr(updates, "urlopen", lambda request, timeout: _fake_github({
        "tag_name": "v0.11.0", "prerelease": False, "draft": False,
        "assets": [{"name":"SHA256SUMS"}, {"name":"release.json"}]
    }))
    found = updates.release_status("0.10.0")
    assert found["engine_update_available"] is True
    assert found["desktop_in_release"] is False and found["desktop_update_available"] is False
    assert found["engine_only"] is True


def test_desktop_and_engine_newer_stable_release_both_offer_update(monkeypatch, tmp_path):
    version = tmp_path / "desktop-version.json"
    version.write_text('{"version":"0.11.0"}')
    monkeypatch.setattr(updates, "VERSION_FILE", version)
    monkeypatch.setattr(updates, "urlopen", lambda request, timeout: _fake_github({
        "tag_name": "v0.13.0", "prerelease": False, "draft": False,
        "assets": [{"name":"SHA256SUMS"}, {"name":"release.json"},
                   {"name":"CodingBrain-Setup.exe"}]
    }))
    found = updates.release_status("0.12.0")
    assert found["engine_update_available"] is True
    assert found["desktop_update_available"] is True and found["desktop_in_release"] is True
    assert found["tag"] == "v0.13.0"


@pytest.mark.parametrize("tag", ["v0.13.0.dev5", "main", "vnot-a-version"])
def test_draft_preview_and_invalid_tags_cannot_drive_stable_update(monkeypatch, tag):
    monkeypatch.setattr(updates, "urlopen", lambda request, timeout: _fake_github({
        "tag_name": tag, "prerelease": True if "dev" in tag else False, "draft": False, "assets": []
    }))
    with pytest.raises(ValueError):
        updates.release_status("0.12.0")


def test_update_script_pins_stable_release_and_verified_preview_artifact():
    script = Path(__file__).resolve().parents[1] / "desktop_ui" / "Update-CodingBrain.ps1"
    source = script.read_text(encoding="utf-8")
    assert "releases/latest" in source and "releases/tags/$tag" in source
    assert "SHA256SUMS" in source and "Verify-Asset 'CodingBrain-Setup.exe'" in source
    assert "update --version $version" in source and "update --source $source" in source
    assert "Read-Host 'Press Enter after closing the desktop window'" in source
    assert "ValidateSet('stable', 'preview')" in source
    assert "head_sha -ceq $head" in source and "$_.conclusion -ceq 'success'" in source
    assert "Installed engine $installed is newer" in source
    assert not re.search(r"\\b(?:Invoke-Expression|iex)\\b", source, re.I)


def test_preview_success_belongs_to_current_branch_head(monkeypatch, tmp_path):
    head = "a" * 40
    monkeypatch.setattr(updates, "VERSION_FILE", tmp_path / "no-version.json")

    def github(url):
        if "/git/ref/heads/preview" in url:
            return {"object": {"sha": head}}
        if "/workflows/preview-build.yml/runs?" in url:
            return {"workflow_runs": [{
                "name": updates.PREVIEW_WORKFLOW_NAME, "head_branch": "preview",
                "head_sha": head, "status": "completed", "conclusion": "success",
                "run_number": 7, "id": 123,
                "html_url": "https://github.com/jeanyves777/codingagent/actions/runs/123",
            }]}
        if "/actions/runs/123/artifacts" in url:
            return {"artifacts": [{"name": "CodingBrain-Preview-0.13.0.dev7", "expired": False}]}
        raise AssertionError(url)

    monkeypatch.setattr(updates, "_json", github)
    result = updates.release_status("0.11.0", channel="preview")
    assert result["unreleased"] is True and result["channel"] == "preview"
    assert result["latest"] == "0.13.0.dev7"
    assert result["preview_run_id"] == 123 and result["preview_source_commit"] == head
    assert result["engine_update_available"] and result["desktop_update_available"]
    assert "releases/tag/" not in result["release_url"]


@pytest.mark.parametrize("problem", ["stale", "failed", "missing", "expired"])
def test_preview_denies_stale_failing_or_expired_ci(monkeypatch, problem):
    head = "a" * 40

    def github(url):
        if "/git/ref/heads/preview" in url:
            return {"object": {"sha": head}}
        if "/workflows/preview-build.yml/runs?" in url:
            return {"workflow_runs": [{
                "name": updates.PREVIEW_WORKFLOW_NAME, "head_branch": "preview",
                "head_sha": "b"*40 if problem == "stale" else head,
                "status": "completed", "conclusion": "failure" if problem == "failed" else "success",
                "run_number": 7, "id": 123,
            }]}
        if "/actions/runs/123/artifacts" in url:
            return {"artifacts": [] if problem == "missing" else
                    [{"name": "CodingBrain-Preview-0.13.0.dev7", "expired": problem == "expired"}]}
        raise AssertionError(url)

    monkeypatch.setattr(updates, "_json", github)
    with pytest.raises(ValueError):
        updates.release_status("0.11.0", channel="preview")


def test_stable_release_not_offered_as_downgrade_from_preview(monkeypatch, tmp_path):
    local = tmp_path / "desktop.json"
    local.write_text('{"version":"0.13.0.dev5"}')
    monkeypatch.setattr(updates, "VERSION_FILE", local)
    monkeypatch.setattr(updates, "_json", lambda url: {
        "tag_name": "v0.11.0", "prerelease": False, "draft": False,
        "assets": [{"name": "SHA256SUMS"}, {"name": "release.json"}]
    })
    result = updates.release_status("0.13.0.dev5", "stable")
    assert not result["any_update_available"]


def test_unsupported_channel_is_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        updates.release_status("0.11.0", channel="arbitrary")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell")
def test_updater_has_valid_windows_powershell_syntax():
    script = Path(__file__).resolve().parents[1] / "desktop_ui" / "Update-CodingBrain.ps1"
    escaped = str(script).replace("'", "''")
    command = ("$tokens=$null;$errors=$null;"
               f"$null=[System.Management.Automation.Language.Parser]::ParseFile('{escaped}',"
               "[ref]$tokens,[ref]$errors);"
               "if($errors.Count){$errors|ForEach-Object{Write-Error $_.Message};exit 1}")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="native Windows console only")
def test_desktop_update_button_hands_off_to_pinned_dual_component_updater(tmp_path, monkeypatch):
    from desktop_ui import management
    from desktop_ui.server import Workspace
    w = Workspace(home=tmp_path)
    engine = str(tmp_path / "installed-python.exe")
    monkeypatch.setattr(w, "cli_command", lambda: [engine, "-m", "brain.local"])
    called = []
    monkeypatch.setattr(management, "launch_interactive_windows", lambda args: called.append(args))
    info = w.maintenance("update_engine")
    assert info["opened_terminal"] is True
    assert len(called) == 1
    assert called[0][:4] == ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass"]
    assert "-EnginePython" in called[0] and engine in called[0]
    assert called[0][called[0].index("-File")+1].endswith("Update-CodingBrain.ps1")
    assert called[0][-2:] == ["-Channel", "stable"]
    w.maintenance("update_engine", channel="preview")
    assert called[1][-2:] == ["-Channel", "preview"]
