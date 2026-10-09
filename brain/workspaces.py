"""Safe task workspaces backed by Git worktrees when possible."""
import shutil
import subprocess
from pathlib import Path
from .repository import snapshot


class WorkspaceManager:
    def _git(self, repository: Path, *arguments: str, timeout: int = 30) -> str:
        result = subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode:
            raise ValueError(result.stderr.strip() or "Git operation failed")
        return result.stdout.strip()

    def is_git(self, repository: Path) -> bool:
        try:
            return self._git(repository, "rev-parse", "--is-inside-work-tree") == "true"
        except (ValueError, OSError, subprocess.TimeoutExpired):
            return False

    def prepare(self, repository: Path, destination: Path, base: str | None = None) -> dict:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.is_git(repository):
            status = self._git(repository, "status", "--porcelain", "--untracked-files=no")
            if status:
                raise ValueError("Git repository has tracked local changes; commit or stash them first")
            commit = base or self._git(repository, "rev-parse", "HEAD")
            self._git(repository, "worktree", "add", "--detach", str(destination), commit)
            return {"kind": "git", "base_commit": commit}
        snapshot(repository, destination)
        return {"kind": "snapshot", "base_commit": None}

    def commit(self, workspace: Path, task_id: str) -> str:
        if not self.is_git(workspace):
            raise ValueError("Commits require a Git-backed workspace")
        self._git(workspace, "add", "--all")
        if not self._git(workspace, "status", "--porcelain"):
            raise ValueError("No changes to commit")
        result = subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Coding Brain",
                                 "-c", "user.email=coding-brain@localhost", "commit", "-m",
                                 f"coding-brain: {task_id}"], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError(result.stderr.strip() or "Commit failed")
        return self._git(workspace, "rev-parse", "HEAD")

    def integration(self, repository: Path, destination: Path, base: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._git(repository, "worktree", "add", "--detach", str(destination), base)

    def integrate(self, workspace: Path, commit: str) -> str:
        result = subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Coding Brain",
                                 "-c", "user.email=coding-brain@localhost", "cherry-pick", commit],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            subprocess.run(["git", "-C", str(workspace), "cherry-pick", "--abort"],
                           capture_output=True, timeout=15)
            raise ValueError(result.stderr.strip() or "Integration conflict")
        return self._git(workspace, "rev-parse", "HEAD")

    def keep(self, repository: Path, ref: str, commit: str) -> None:
        """Pin a commit with a private ref so removing its worktree cannot let Git collect it."""
        self._git(repository, "update-ref", ref, commit)

    def remove(self, repository: Path, workspace: Path) -> None:
        if self.is_git(workspace):
            self._git(repository, "worktree", "remove", "--force", str(workspace))
        elif workspace.exists():
            shutil.rmtree(workspace)
