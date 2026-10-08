"""Phase 3 verification and fallback through GitHub: publish verified work as a draft pull
request, collect CI checks and review feedback, and turn that feedback into follow-up work for the
free implementer. GitHub verifies and reports; it does not write code."""
import asyncio
import re
import httpx

REMOTE = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
FAILING = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}


def parse_remote(url: str) -> tuple[str, str]:
    match = REMOTE.search(url.strip())
    if not match:
        raise ValueError("Remote is not a GitHub repository; pass github_repository as owner/name")
    return match.group(1), match.group(2)


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com", transport=None):
        if not token:
            raise ValueError("A GitHub token is required")
        self.token, self.api_url, self.transport = token, api_url.rstrip("/"), transport

    def _client(self):
        return httpx.AsyncClient(base_url=self.api_url, timeout=30, transport=self.transport, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})

    async def _request(self, method: str, path: str, **kwargs):
        async with self._client() as client:
            response = await client.request(method, path, **kwargs)
            if response.status_code >= 400:
                raise ValueError(f"GitHub {method} {path} failed with {response.status_code}: "
                                 f"{response.text[:300]}")
            return response.json()

    async def create_pull(self, owner, repo, head, base, title, body, draft=True) -> dict:
        return await self._request("POST", f"/repos/{owner}/{repo}/pulls", json={
            "title": title, "head": head, "base": base, "body": body, "draft": draft})

    async def feedback(self, owner, repo, number: int, sha: str) -> dict:
        prefix = f"/repos/{owner}/{repo}"
        checks, status, reviews, comments, discussion = await asyncio.gather(
            self._request("GET", f"{prefix}/commits/{sha}/check-runs", params={"per_page": 100}),
            self._request("GET", f"{prefix}/commits/{sha}/status"),
            self._request("GET", f"{prefix}/pulls/{number}/reviews", params={"per_page": 100}),
            self._request("GET", f"{prefix}/pulls/{number}/comments", params={"per_page": 100}),
            self._request("GET", f"{prefix}/issues/{number}/comments", params={"per_page": 100}))
        runs = [{"name": run["name"], "status": run["status"], "conclusion": run.get("conclusion"),
                 "summary": ((run.get("output") or {}).get("summary") or "")[:2000]}
                for run in checks.get("check_runs", [])]
        runs += [{"name": item["context"], "status": "completed", "conclusion": item["state"],
                  "summary": (item.get("description") or "")[:2000]}
                 for item in status.get("statuses", [])]
        pending = [run for run in runs if run["status"] != "completed" or run["conclusion"] == "pending"]
        failing = [run for run in runs if run["conclusion"] in FAILING | {"error"}]
        notes = ([{"author": item["user"]["login"], "state": item["state"], "body": item.get("body") or ""}
                  for item in reviews if item.get("body") or item["state"] == "CHANGES_REQUESTED"] +
                 [{"author": item["user"]["login"], "path": item.get("path"), "line": item.get("line"),
                   "body": item["body"]} for item in comments] +
                 [{"author": item["user"]["login"], "body": item["body"]} for item in discussion])
        return {"sha": sha, "checks": runs, "failing": failing, "pending": len(pending),
                "changes_requested": any(item["state"] == "CHANGES_REQUESTED" for item in reviews),
                "comments": notes[-30:]}
