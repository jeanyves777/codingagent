Build the complete Windows system from the accepted checkout with:

```text
python scripts/build_windows_system.py --output <artifact-directory> --version <new-version>
```

The script builds the matching engine wheel, native desktop, installer, and full-system ZIP;
it verifies native assets, versions and package checksums. It uses an isolated build environment
and downloads build dependencies. Inno Setup 6 is required; provide `--compiler <ISCC.exe>`
if it is outside the usual locations. `--build-python` and `--constraints` may use an existing,
verified build environment and dependency lock file.

For a local self-update, run the tested engine updater against the generated `engine` directory,
then run the generated `CodingBrain-Setup.exe` to replace the native UI too. Save unsaved editor
buffers before applying it. Reopen the installed desktop and verify its displayed engine version
and the changed feature. The installer stops only verified Coding Brain holders of its own files.

The engine supports goal-derived lifecycle recipes in proposals. Each step names a build,
verification, application or launch command and the inspected project files supporting it.
After code tests and acceptance, the UI shows the resolved commands for separate approval.
Commands run against the accepted workspace, with artifact hashes checked before application.
If project tests require dependencies or a toolchain image, the recipe can include separate
environment preparation. Its exact commands require approval before testing; it cannot modify
the project source. A task-specific sandbox image can be selected without changing global settings.
Results persist in the task's workflow receipt across engine/UI restarts. Background launch alone
is not readiness proof: a subsequent foreground verification step is required.

This is the same recipe mechanism for every project. Its runtime may need tools not installed
yet; a missing tool or failed command is reported as a failure, not as a working result.
