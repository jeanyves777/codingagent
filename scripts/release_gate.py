"""Decide whether a commit may be released. Run by .github/workflows/release.yml before the
owner's approval; any failed condition stops the release and nothing is tagged or published.

    python scripts/release_gate.py --sha <40-hex> --version 0.9.1 --repo owner/name

A candidate is releasable only when all of these hold:
  1. the SHA is a full commit id on main;
  2. pyproject.toml at that commit says exactly this version, and CHANGELOG.md has its section;
  3. the tag v<version> does not exist yet, and the version is newer than the latest release;
  4. the CI workflow passed on that exact code: every check of the "Windows local install"
     workflow succeeded (at least the Windows verify matrix and the real Docker sandbox), on the
     commit itself or on the pull-request head whose tree is identical (a merge commit of an
     up-to-date branch has the same tree, so the same code was tested);
  5. the "production" environment requires a reviewer, so publishing waits for the owner.
The script only reads (Git and the GitHub API); it never tags, pushes or publishes.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

WORKFLOW = "Windows local install"
REQUIRED_PREFIXES = ("verify (", "real Docker sandbox")
IGNORED = {"release"}  # the tag-triggered release job is skipped on pull requests


class Refused(Exception):
    pass


def git(*arguments: str, check: bool = True) -> str:
    result = subprocess.run(["git", *arguments], capture_output=True, text=True)
    if check and result.returncode:
        raise Refused(f"git {' '.join(arguments)} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip() if result.returncode == 0 else ""


def github(repo: str, token: str):
    def api(path: str):
        request = urllib.request.Request(f"https://api.github.com/repos/{repo}/{path}", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise Refused(f"GitHub API {path}: HTTP {error.code}") from error
    return api


def version_key(version: str) -> tuple:
    from packaging.version import Version
    return Version(version)


def ci_conclusions(api, sha: str) -> dict[str, str]:
    """Latest conclusion per check name of the CI workflow on this commit."""
    runs = (api(f"commits/{sha}/check-runs?per_page=100") or {}).get("check_runs", [])
    latest = {}
    for run in runs:
        if (run.get("app") or {}).get("slug") != "github-actions" or run["name"] in IGNORED:
            continue  # only GitHub Actions runs count; another app cannot post a passing check
        previous = latest.get(run["name"])
        if previous is None or (run.get("completed_at") or "") > (previous.get("completed_at") or ""):
            latest[run["name"]] = run
    return {name: (run.get("conclusion") or run.get("status")) for name, run in latest.items()}


def ci_evidence(api, sha: str, tree_of) -> tuple[str, dict]:
    """(tested commit, conclusions): the commit itself, or a merged PR head with the same tree."""
    candidates = [sha]
    for pull in api(f"commits/{sha}/pulls") or []:
        if pull.get("merged_at") and pull.get("merge_commit_sha") == sha:
            candidates.append(pull["head"]["sha"])
    for commit in candidates:
        conclusions = ci_conclusions(api, commit)
        if not conclusions:
            continue
        if commit != sha and tree_of(commit) != tree_of(sha):
            continue
        return commit, conclusions
    raise Refused(f"no CI results for {sha} or for a pull-request head with the identical tree")


def check_ci(conclusions: dict) -> list[str]:
    problems = [f"{name}: {state}" for name, state in sorted(conclusions.items()) if state not in {"success", "skipped"}]
    for prefix in REQUIRED_PREFIXES:
        if not any(name.startswith(prefix) and state == "success" for name, state in conclusions.items()):
            problems.append(f"no successful '{prefix}…' check")
    return problems


def check(sha: str, version: str, repo: str, api, tree_of=None, show=None, ancestor=None, latest=None) -> dict:
    tree_of = tree_of or (lambda commit: git("rev-parse", f"{commit}^{{tree}}"))
    show = show or (lambda commit, path: git("show", f"{commit}:{path}"))
    ancestor = ancestor or (lambda commit: subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "origin/main"]).returncode == 0)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Refused("the SHA must be the full 40-character commit id")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc|\.dev|\.post)\d+)?", version):
        raise Refused(f"'{version}' is not a release version")
    if not ancestor(sha):
        raise Refused(f"{sha} is not on main")
    found = re.search(r'^version = "([^"]+)"', show(sha, "pyproject.toml"), re.M)
    if not found or found.group(1) != version:
        raise Refused(f"pyproject.toml at {sha[:12]} says {found and found.group(1)}, not {version}")
    if not re.search(rf"^## {re.escape(version)}\b", show(sha, "CHANGELOG.md"), re.M):
        raise Refused(f"CHANGELOG.md at {sha[:12]} has no '## {version}' section (it becomes the release notes)")
    if api(f"git/ref/tags/v{version}"):
        raise Refused(f"tag v{version} already exists")
    published = latest if latest is not None else (api("releases/latest") or {}).get("tag_name")
    if published and version_key(version) <= version_key(published.lstrip("v")):
        raise Refused(f"{version} is not newer than the latest release {published}")
    tested, conclusions = ci_evidence(api, sha, tree_of)
    problems = check_ci(conclusions)
    if problems:
        raise Refused("CI did not pass on this code: " + "; ".join(problems))
    environment = api("environments/production")
    rules = (environment or {}).get("protection_rules") or []
    if not any(rule.get("type") == "required_reviewers" and rule.get("reviewers") for rule in rules):
        raise Refused("the 'production' environment must exist and require a reviewer (Settings > Environments), "
                      "so that publishing waits for the owner's approval")
    return {"sha": sha, "version": version, "tested_commit": tested, "checks": conclusions}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    try:
        result = check(args.sha.strip().lower(), args.version.strip(), args.repo,
                       github(args.repo, os.environ["GH_TOKEN"]))
    except Refused as error:
        print(f"Release refused: {error}")
        return 1
    print(json.dumps(result, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Release candidate v{result['version']}\n\nCommit `{result['sha']}`, CI evidence from "
                         f"`{result['tested_commit']}`:\n\n" + "\n".join(
                             f"- {name}: {state}" for name, state in sorted(result["checks"].items())) +
                         "\n\nApproving the `production` deployment tags this commit and publishes the release.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
