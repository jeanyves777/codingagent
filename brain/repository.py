from pathlib import Path
import shutil

EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".toml", ".md", ".txt", ".yaml", ".yml", ".css", ".html"}
MAX_FILE = 200_000
MAX_TOTAL = 20_000_000
PROTECTED = {"coding-brain.json"}


def safe_path(root: Path, name: str, allow_protected=False) -> Path:
    path = Path(name)
    if path.is_absolute() or not path.parts or any(
        part in {"..", "."} or part.startswith(".") or part in EXCLUDED
        for part in path.parts
    ):
        raise ValueError("Path is outside the allowed source tree")
    if path.as_posix() in PROTECTED and not allow_protected:
        raise ValueError("Path is controlled by the repository owner")
    if path.suffix not in SUFFIXES:
        raise ValueError("Unsupported source file type")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks are not allowed")
    resolved = current.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Path escapes repository")
    return resolved


def snapshot(source: Path, destination: Path) -> None:
    total = 0
    count = 0
    destination.mkdir(parents=True)
    # Prune directories before descending; never follow directory symlinks.
    import os
    for base, directories, files in os.walk(source, followlinks=False):
        directories[:] = sorted(d for d in directories if not d.startswith(".")
                                 and d not in EXCLUDED and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            file = Path(base) / name
            if name.startswith(".") or file.suffix not in SUFFIXES or file.is_symlink():
                continue
            # Explicitly exclude common credential containers, even with text suffixes.
            if any(word in name.lower() for word in ("secret", "credential", "private_key")):
                continue
            size = file.stat().st_size
            if size > MAX_FILE:
                raise ValueError(f"Source file exceeds limit: {file.relative_to(source)}")
            total += size
            count += 1
            if total > MAX_TOTAL or count > 1000:
                raise ValueError("Repository exceeds snapshot limits")
            relative = file.relative_to(source)
            target = safe_path(destination, relative.as_posix(), allow_protected=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)


def inspect(root: Path, tool: str, arguments: dict) -> str:
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    if tool == "list_files":
        return "\n".join(files)[:16_000]
    if tool == "read_file":
        return safe_path(root, arguments["path"]).read_text(encoding="utf-8")[:16_000]
    if tool == "search":
        query = arguments["query"]
        if not isinstance(query, str) or not query or len(query) > 200:
            raise ValueError("Search query must contain 1–200 characters")
        hits = []
        for name in files:
            try:
                lines = safe_path(root, name).read_text(encoding="utf-8").splitlines()
            except UnicodeError:
                continue
            for number, line in enumerate(lines, 1):
                if query in line:
                    hits.append(f"{name}:{number}: {line[:300]}")
                    if len(hits) >= 40:
                        return "\n".join(hits)
        return "\n".join(hits)
    raise ValueError("Unknown tool")
