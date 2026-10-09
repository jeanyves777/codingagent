"""Recognize the project Coding Brain was started in. Read-only: nothing here writes to it."""
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

LANGUAGES = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
             ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".kt": "Kotlin", ".cs": "C#",
             ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".cpp": "C++", ".cc": "C++",
             ".c": "C", ".h": "C/C++", ".swift": "Swift", ".scala": "Scala", ".sql": "SQL",
             ".ps1": "PowerShell", ".sh": "Shell", ".vue": "Vue", ".svelte": "Svelte", ".dart": "Dart"}
SKIP = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target", ".next",
        ".tox", ".mypy_cache", ".pytest_cache", "bin", "obj", ".idea", ".vscode", "vendor", "coverage"}
MANAGERS = {"package-lock.json": "npm", "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "bun.lockb": "bun",
            "poetry.lock": "poetry", "uv.lock": "uv", "Pipfile": "pipenv", "requirements.txt": "pip",
            "pyproject.toml": "pip/pyproject", "Cargo.toml": "cargo", "go.mod": "go modules", "pom.xml": "maven",
            "build.gradle": "gradle", "build.gradle.kts": "gradle", "Gemfile": "bundler",
            "composer.json": "composer", "packages.config": "nuget", "Directory.Packages.props": "nuget"}
FRAMEWORKS = {"django": "Django", "flask": "Flask", "fastapi": "FastAPI", "pytest": "pytest",
              "react": "React", "next": "Next.js", "vue": "Vue", "@angular/core": "Angular", "express": "Express",
              "svelte": "Svelte", "jest": "Jest", "vitest": "Vitest", "typescript": "TypeScript",
              "spring-boot": "Spring Boot", "rails": "Rails", "laravel/framework": "Laravel"}
DOCS = ("README.md", "README.rst", "README.txt", "README", "CONTRIBUTING.md", "ARCHITECTURE.md", "CLAUDE.md",
        "AGENTS.md", "docs", "CHANGELOG.md")


def _git(root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def find_root(start: Path) -> Path:
    """The Git work-tree root containing start, or start itself."""
    top = _git(start, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else start.resolve()


def project_id(root: Path) -> str:
    """Stable per-project identity: the resolved path (case-insensitive on Windows)."""
    key = str(root.resolve())
    key = key.lower() if sys.platform == "win32" else key
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", root.name).strip("-")[:40] or "project"
    return f"{slug}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def _files(root: Path, limit: int = 5000):
    stack, seen = [root], 0
    while stack and seen < limit:
        folder = stack.pop()
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in SKIP and not entry.name.startswith("."):
                    stack.append(entry)
            else:
                seen += 1
                yield entry


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def commands(root: Path) -> dict:
    """Build and test commands the project already declares."""
    found = {}
    package = _read_json(root / "package.json")
    for name in ("build", "test", "lint", "start", "dev"):
        if name in (package.get("scripts") or {}):
            manager = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm"
            found[name] = f"{manager} run {name}" if name != "test" else f"{manager} test"
    if (root / "pyproject.toml").exists() or (root / "setup.cfg").exists() or (root / "pytest.ini").exists() \
            or any(root.glob("test_*.py")) or (root / "tests").is_dir():
        if any((root / name).exists() for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")) \
                or (root / "tests").is_dir() or any(root.glob("test_*.py")):
            found.setdefault("test", "python -m pytest")
    if (root / "Cargo.toml").exists():
        found.setdefault("build", "cargo build")
        found.setdefault("test", "cargo test")
    if (root / "go.mod").exists():
        found.setdefault("build", "go build ./...")
        found.setdefault("test", "go test ./...")
    if (root / "pom.xml").exists():
        found.setdefault("build", "mvn package")
        found.setdefault("test", "mvn test")
    if any(root.glob("*.sln")) or any(root.glob("*.csproj")):
        found.setdefault("build", "dotnet build")
        found.setdefault("test", "dotnet test")
    makefile = root / "Makefile"
    if makefile.exists():
        targets = re.findall(r"^([A-Za-z][\w-]*):", makefile.read_text(encoding="utf-8", errors="replace"), re.M)
        for name in ("build", "test"):
            if name in targets:
                found.setdefault(name, f"make {name}")
    config = _read_json(root / "coding-brain.json")
    if config.get("test_command"):
        found["test"] = " ".join(config["test_command"])  # what Coding Brain's sandbox will run
    return found


def frameworks(root: Path) -> list[str]:
    names = set()
    package = _read_json(root / "package.json")
    for section in ("dependencies", "devDependencies"):
        names.update((package.get(section) or {}).keys())
    for manifest in ("requirements.txt", "pyproject.toml", "Pipfile", "Gemfile", "composer.json", "pom.xml"):
        path = root / manifest
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            names.update(key for key in FRAMEWORKS if re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", text))
    return sorted({FRAMEWORKS[key] for key in names if key in FRAMEWORKS})


def detect(start: Path) -> dict:
    root = find_root(start)
    counts = Counter(LANGUAGES[path.suffix.lower()] for path in _files(root) if path.suffix.lower() in LANGUAGES)
    git = _git(root, "rev-parse", "--is-inside-work-tree") == "true"
    status = _git(root, "status", "--porcelain") if git else None
    return {
        "root": str(root), "name": root.name, "id": project_id(root), "git": git,
        "branch": (_git(root, "branch", "--show-current") or "(detached)") if git else None,
        "head": _git(root, "rev-parse", "--short", "HEAD") if git else None,
        "changes": len(status.splitlines()) if status else 0,
        "tracked_changes": bool(_git(root, "status", "--porcelain", "--untracked-files=no")) if git else False,
        "languages": [name for name, _ in counts.most_common(6)],
        "frameworks": frameworks(root),
        "dependency_managers": sorted({manager for name, manager in MANAGERS.items() if (root / name).exists()}),
        "docs": [name for name in DOCS if (root / name).exists()],
        "commands": commands(root),
    }
