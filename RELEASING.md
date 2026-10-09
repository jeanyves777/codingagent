# Releasing Coding Brain

Application versions are independent of benchmark configurations: a benchmark records the exact
commit it ran (its fingerprint). A benchmark-approved improvement reaches users as a normal
release, through these steps.

1. **Version.** Bump `version` in `pyproject.toml`, following semantic versioning.
   Pre-releases (`1.2.0rc1`, `1.2.0.dev3`) go to the dev channel only.
2. **State changes.** If configuration or stored state changes shape:
   - increase `SCHEMA_VERSION` in `brain/local/config.py`;
   - add an idempotent step to `brain/local/migrations.py` (`STEPS[old] = function`);
   - add a test that migrates a copy of the old state.

   Updates back up state before migrating and restore it on failure. If a release cannot upgrade
   very old installs directly, raise `MIN_UPGRADE_FROM` in `scripts/build_release.py`. The updater
   then tells those users which intermediate version to install first.
3. **Release notes.** Add a `## <version>` section to `CHANGELOG.md`. It becomes the release
   notes that `codingbrain update` displays, and should include any migration instructions.
4. **Verify.** On a pull request, the Windows workflow runs the local-install unit tests, then
   `installer/windows/verify.ps1` on Windows Server 2022 and 2025 with Windows PowerShell 5.1 and
   PowerShell 7. The script covers:
   - fresh install;
   - recognition of an unrelated repository;
   - health check;
   - update;
   - refusal of a tampered update;
   - refusal of an update during a session;
   - rollback;
   - uninstall with data kept, and full removal.
5. **Tag.** Merge to `main`, then tag `v<version>` and push the tag. The `release` job:
   - checks that the tag matches `pyproject.toml`;
   - builds the wheel and resolves `constraints.txt` on Windows;
   - writes `release.json` and `SHA256SUMS`;
   - attests build provenance;
   - publishes the GitHub release. A pre-release is marked as such and never becomes "latest".

Build the assets locally with `python scripts/build_release.py --out dist/release`.
