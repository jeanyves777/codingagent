"""Side-by-side views of a proposal's unified diff, for the desktop's diff editor.

Read-only and display-only: nothing here writes files. Each changed file gets the text before and
after the change. When the project's current file matches the diff's context, the full file is
shown with the hunks applied ("exact"); otherwise only the changed regions are shown, built from
the hunk lines themselves, so the view never invents content.
"""
from __future__ import annotations

import re
from pathlib import Path

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
MAX_FILES = 100
MAX_FILE_BYTES = 1_000_000


def _strip(path: str) -> str | None:
    path = path.strip().split("\t")[0]
    if path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def parse(diff: str) -> list[dict]:
    """Files of a unified (git) diff: {old, new, hunks: [{old_start, lines: [(tag, text)], complete}]}.

    Each hunk's remaining old/new line counts come from its "@@ -a,b +c,d @@" header, so a removed
    line that itself starts with "-- " (an SQL, Lua or Haskell comment) is read as hunk content, never
    as the next file's header. A hunk that ends early (a cut-off diff) is marked incomplete."""
    files, current, hunk, old_left, new_left = [], None, None, 0, 0
    for line in diff.splitlines():
        in_hunk = hunk is not None and (old_left > 0 or new_left > 0)
        if in_hunk:
            tag = line[:1]
            if tag in (" ", ""):  # context ("" when an editor stripped the blank context line)
                hunk["lines"].append((" ", line[1:]))
                old_left, new_left = old_left - 1, new_left - 1
                continue
            if tag == "-":
                hunk["lines"].append(("-", line[1:]))
                old_left -= 1
                continue
            if tag == "+":
                hunk["lines"].append(("+", line[1:]))
                new_left -= 1
                continue
            if line.startswith("\\"):
                continue  # the "no newline at end of file" marker
            hunk["complete"] = False  # something else interrupted the hunk
            in_hunk = False
        if hunk is not None and not in_hunk and line.startswith("\\"):
            continue
        if line.startswith("diff --git "):
            current, hunk = None, None
            continue
        if line.startswith("--- "):
            current = {"old": _strip(line[4:]), "new": None, "hunks": []}
            files.append(current)
            hunk = None
            continue
        if line.startswith("+++ ") and current is not None and current["new"] is None and not current["hunks"]:
            current["new"] = _strip(line[4:])
            continue
        match = HUNK.match(line)
        if match and current is not None:
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            new_left = int(match.group(4)) if match.group(4) is not None else 1
            hunk = {"old_start": int(match.group(1)), "lines": [], "complete": True}
            current["hunks"].append(hunk)
    if hunk is not None and (old_left > 0 or new_left > 0):
        hunk["complete"] = False  # the diff ended inside this hunk
    return [item for item in files if item["hunks"] or item["old"] != item["new"]][:MAX_FILES]


def _apply(original: list[str], hunks: list[dict]) -> list[str] | None:
    """The original lines with the hunks applied, or None when the context does not match."""
    result, cursor = [], 0
    for hunk in hunks:
        start = max(hunk["old_start"] - 1, 0)
        if start < cursor:
            return None
        result.extend(original[cursor:start])
        position = start
        for tag, text in hunk["lines"]:
            if tag in (" ", "-"):
                if position >= len(original) or original[position] != text:
                    return None
                position += 1
                if tag == " ":
                    result.append(text)
            else:
                result.append(text)
        cursor = position
    result.extend(original[cursor:])
    return result


def _regions(hunks: list[dict]) -> tuple[str, str]:
    before, after = [], []
    for index, hunk in enumerate(hunks):
        if index:
            before.append("…")
            after.append("…")
        for tag, text in hunk["lines"]:
            if tag in (" ", "-"):
                before.append(text)
            if tag in (" ", "+"):
                after.append(text)
    return "\n".join(before), "\n".join(after)


def views(diff: str, root: Path | None, allowed=lambda path: True, truncated: bool = False) -> list[dict]:
    """One {path, status, original, modified, exact} per changed file. `allowed(relative_path)` decides
    whether the project's copy of a file may be read (the desktop never reads secrets or links)."""
    out = []
    for item in parse(diff):
        path = item["new"] or item["old"] or "?"
        status = "added" if item["old"] is None else "deleted" if item["new"] is None else "modified"
        text = None
        if status != "added" and root is not None and allowed(item["old"]):
            candidate = (root / item["old"]).resolve()
            try:
                if candidate.is_relative_to(root.resolve()) and candidate.is_file() and \
                        candidate.stat().st_size <= MAX_FILE_BYTES and not candidate.is_symlink():
                    text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError):
                text = None
        if status == "added":
            modified = [t for hunk in item["hunks"] for tag, t in hunk["lines"] if tag in ("+", " ")]
            out.append({"path": path, "status": status, "original": "", "modified": "\n".join(modified), "exact": True})
            continue
        complete = all(hunk.get("complete", True) for hunk in item["hunks"])
        if text is not None and complete:
            lines = text.splitlines()
            applied = _apply(lines, item["hunks"])
            if applied is not None:
                trailing = "\n" if text.endswith("\n") else ""
                out.append({"path": path, "status": status, "original": text,
                            "modified": ("\n".join(applied) + trailing) if status != "deleted" else "", "exact": True})
                continue
        original, modified = _regions(item["hunks"])
        out.append({"path": path, "status": status, "original": original,
                    "modified": modified if status != "deleted" else "", "exact": False})
    for view, item in zip(out, parse(diff)):
        if not all(hunk.get("complete", True) for hunk in item["hunks"]):
            view["exact"] = False
            view["note"] = "The proposal's diff for this file is incomplete; only the part received is shown."
    if truncated and out:
        out[-1]["exact"] = False
        out[-1]["note"] = "The proposal's diff was cut at 100,000 characters; later changes are not shown."
    return out
