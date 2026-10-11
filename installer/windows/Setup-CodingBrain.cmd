@echo off
rem Started through Explorer by CodingBrain-Setup.exe, so setup runs like a program the user opened,
rem without the installer's process restrictions (Inno Setup enforces Windows redirection trust,
rem which blocks the user-created junctions that tools such as the Codex CLI install through).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup-CodingBrain.ps1" -Bundle "%~dp0." -Desktop "%~dp0..\CodingBrainDesktop.exe"
