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
5. **Publish (owner-approved, recommended).** See *Owner-approved releases* below. Alternatively, merge to `main`, then tag `v<version>` and push the tag. The `release` job:
   - checks that the tag matches `pyproject.toml`;
   - builds the wheel and resolves `constraints.txt` on Windows;
   - writes `release.json` and `SHA256SUMS`;
   - attests build provenance;
   - publishes the GitHub release. A pre-release is marked as such and never becomes "latest".

Build the assets locally with `python scripts/build_release.py --out dist/release`.

## Owner-approved releases (`.github/workflows/release.yml`)

Releases can be published without pushing a tag from a developer machine. The workflow tags and
publishes one exact commit of `main`, and only after the repository owner approves the
`production` deployment in GitHub.

**One-time setup (owner, in GitHub):**

1. Open Settings > Environments > New environment, and name it `production`.
2. Add yourself under **Required reviewers**.
3. Optionally, restrict deployment branches to `main`.

The workflow refuses to run if this environment does not require a reviewer.

**Each release:**

1. **Start the workflow.** Go to Actions > *Release (owner-approved)* > Run workflow, and enter:
   - the full commit SHA on `main`;
   - the version in that commit's `pyproject.toml`.

   An agent can also start it through the API. Starting the workflow publishes nothing.
2. **Gate (read-only).** `scripts/release_gate.py` runs from `main`, never from the candidate. It
   refuses the release unless all of the following hold:
   - the commit is on `main`;
   - the version matches `pyproject.toml`;
   - `CHANGELOG.md` has a section for it;
   - the tag does not exist yet;
   - the version is newer than the latest release;
   - every check of the *Windows local install* workflow succeeded on this exact code. That
     means the commit itself, or the merged pull request's head when its tree is identical.
     Only GitHub Actions checks count, and at least the Windows verify matrix and the real
     Docker sandbox must be present.

   The run summary lists the evidence.
3. **Approval.** The `publish` job waits for the owner's approval of the `production` deployment
   (Review deployments > Approve). Nothing is tagged or published before that.
4. **Publish.** It checks out the approved SHA, confirms it is that commit and version, and builds
   the assets on Windows (`SHA256SUMS`, `release.json`). It then attests build provenance, creates
   the annotated tag `v<version>` on that commit, and publishes the release.
5. **Confirm.** It downloads the published release, verifies every checksum and the version, and
   checks that the tag points at the approved SHA.

Release in dependency order: one version at a time, each confirmed before the next starts. The
gate's "newer than the latest release" rule enforces the order.

Users then update with one command: `codingbrain update`. It verifies the release checksums,
backs up their state and keeps the previous version for `codingbrain rollback`.
