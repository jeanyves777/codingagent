"""Where Coding Brain keeps its application versions and user state on this computer."""
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_home() -> Path:
    if os.environ.get("CODINGBRAIN_HOME"):
        return Path(os.environ["CODINGBRAIN_HOME"]).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "CodingBrain"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "codingbrain"


@dataclass(frozen=True)
class Layout:
    """home/
         app/versions/<version>/venv   installed application versions, side by side
         app/current.json              the active version and the ones it can roll back to
         bin/codingbrain(.cmd)         the launcher on PATH
         config/                       config.json, brains.json (no credentials)
         data/                         knowledge index, supervision ledger, projects/<id>/
         backups/                      state backups taken before each update
         locks/                        running sessions and updates
    """
    home: Path

    @classmethod
    def default(cls) -> "Layout":
        return cls(default_home())

    @property
    def app(self) -> Path:
        return self.home / "app"

    @property
    def versions(self) -> Path:
        return self.app / "versions"

    @property
    def current_file(self) -> Path:
        return self.app / "current.json"

    @property
    def bin(self) -> Path:
        return self.home / "bin"

    @property
    def config(self) -> Path:
        return self.home / "config"

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def projects(self) -> Path:
        return self.data / "projects"

    @property
    def backups(self) -> Path:
        return self.home / "backups"

    @property
    def locks(self) -> Path:
        return self.home / "locks"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    def ensure(self) -> "Layout":
        for path in (self.config, self.projects, self.backups, self.locks, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def venv_python(self, version: str) -> Path:
        venv = self.versions / version / "venv"
        return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
