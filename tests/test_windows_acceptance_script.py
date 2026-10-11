"""scripts/windows_acceptance.ps1 runs on a user's Windows PC: it must parse, and every Coding Brain
command line in it must be one the real CLI accepts."""
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from brain.local import cli

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "windows_acceptance.ps1"


def command_lines() -> list[list[str]]:
    text = SCRIPT.read_text(encoding="utf-8")
    lines = [match.group(1) for match in re.finditer(r"\bCB ([a-z][^\n]*)", text)]
    lines = ["version" if line.startswith("version)") else line for line in lines]
    lines += [match.group(1) for match in re.finditer(r'"codingbrain ([a-z ]+?)(?: >|")', text)]
    lines += [match.group(1) for match in re.finditer(r"& codingbrain (backlog status --json)", text)]
    calls = []
    for line in lines:
        line = line.replace("$Model", "qwen").replace("$slug", "abc123").split(" 2>")[0]
        calls.append(shlex.split(line))
    return calls


def test_every_command_in_the_acceptance_script_is_accepted_by_the_cli(monkeypatch):
    seen = []
    for name in ("cmd_backlog", "cmd_setup", "cmd_doctor"):
        monkeypatch.setattr(cli, name, lambda args, layout, name=name: seen.append((name, args)) or 0)
    calls = command_lines()
    assert len(calls) >= 9
    for arguments in calls:
        if arguments[0] == "version":
            continue
        if "--help" in arguments:
            with pytest.raises(SystemExit) as exit_info:
                cli.main(arguments)
            assert exit_info.value.code == 0
            continue
        assert cli.main(arguments) == 0, arguments
    commands = {(name, getattr(args, "action", None)) for name, args in seen}
    assert {("cmd_setup", None), ("cmd_doctor", None), ("cmd_backlog", "add"), ("cmd_backlog", "status"),
            ("cmd_backlog", "run"), ("cmd_backlog", "accept")} <= commands
    setup = next(args for name, args in seen if name == "cmd_setup")
    assert setup.execution == "auto" and setup.non_interactive  # acceptance stays manual: only proposals run
    adds = [args for name, args in seen if name == "cmd_backlog" and args.action == "add"]
    assert all(args.accept_file and args.accept_node for args in adds) and adds[-1].depends == ["abc123"]


@pytest.mark.skipif(os.name != "nt", reason="parses the script with Windows PowerShell")
def test_the_acceptance_script_parses_in_windows_powershell():
    escaped = str(SCRIPT).replace("'", "''")
    command = ("$tokens=$null;$errors=$null;"
               f"$null=[System.Management.Automation.Language.Parser]::ParseFile('{escaped}',[ref]$tokens,[ref]$errors);"
               "if($errors.Count){$errors|ForEach-Object{Write-Error $_.Message};exit 1}")
    env = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, result.stderr
