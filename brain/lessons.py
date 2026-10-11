"""Lessons from finished work, for later tasks: what the problem was, why earlier attempts failed,
what change fixed it and how that was verified.

Only facts Coding Brain observed are kept: the goal, the sandbox's failure categories and its own
deterministic diagnoses (for example that tests share state), the files and plan of the change the
user accepted, and the test result. Model opinions are not stored as causes, raw test logs are
cut to one line, and every text is redacted. `relevant` decides which lessons a new goal may see,
so unrelated work never fills the prompt."""
import re

from .redaction import redact

STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "when", "then", "than", "are", "was", "were",
        "will", "should", "must", "have", "has", "not", "all", "any", "add", "make", "use", "new", "each", "its",
        "create", "file", "function", "returns", "return", "test", "tests", "code", "change", "fix", "module",
        "python", "def", "class", "value", "values", "one", "two", "only", "also", "your", "you", "they", "them",
        "accepted", "locally", "problem", "plan", "verified", "failed", "attempt"}


def keywords(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z][a-z0-9_]{2,}", str(text).lower()) if word not in STOP}


def identifiers(text: str) -> set[str]:
    """Names from code (snake_case, camelCase, dotted): one shared name is a strong link."""
    found = re.findall(r"\b[A-Za-z_][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b|\b[a-z]+[A-Z][A-Za-z0-9]*\b|\b\w+\.\w+\b", str(text))
    return {name.lower() for name in found if len(name) > 3}


def relevant(goal: str, text: str) -> bool:
    """A lesson belongs in a new task's context only if it shares real terms with the goal: a
    code identifier, or at least two distinctive words (one when the goal itself has fewer than
    three)."""
    wanted = keywords(goal)
    if not wanted:
        return False
    if identifiers(goal) & identifiers(text):
        return True
    shared = wanted & keywords(text)
    return len(shared) >= (1 if len(wanted) < 3 else 2)


def score(goal: str, text: str) -> int:
    return len(keywords(goal) & keywords(text)) + 2 * len(identifiers(goal) & identifiers(text))


def _line(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return redact(text)[:limit]


def _failure(entry: dict) -> str:
    summary = str(entry.get("summary") or "")
    # the most telling line of a test failure, not the log
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    telling = next((line for line in lines if re.match(r"(E\s|FAILED|AssertionError|\w+Error)", line)), lines[0] if lines else "")
    return _line(f"{entry.get('category') or 'failure'}"
                 + (f" ({entry['diagnosis']})" if entry.get("diagnosis") else "") + f": {telling}", 200)


def lesson(task: dict) -> dict:
    """The structured lesson of a task, accepted or not."""
    failures = [item for item in task.get("failure_log") or [] if isinstance(item, dict)]
    causes = sorted({str(item["diagnosis"]) for item in failures if item.get("diagnosis")})
    proposal = task.get("proposal") or {}
    evidence = task.get("test_evidence") or {}
    output = str(evidence.get("output") or "")
    counted = re.findall(r"\d+ (?:passed|failed|errors?|skipped)", output)
    accepted = task.get("status") == "accepted"
    return {
        "problem": _line(task.get("goal"), 400),
        "root_cause": causes,
        "failed_approaches": [_failure(item) for item in failures][-3:],
        "solution": {"plan": _line(proposal.get("plan"), 300),
                     "files": [change.get("path") for change in proposal.get("changes", [])][:10]} if accepted else None,
        "verification": {"passed": bool(evidence.get("passed")), "exit_code": evidence.get("exit_code"),
                         "result": ", ".join(counted[-3:])},
        "verified": accepted and bool(evidence.get("passed")),
        "status": task.get("status"),
    }


def text(item: dict) -> str:
    """One compact paragraph: what later prompts and keyword retrieval use."""
    parts = [f"Problem: {item['problem']}"]
    if item.get("root_cause"):
        parts.append("Cause found: " + ", ".join(item["root_cause"]))
    if item.get("failed_approaches"):
        parts.append("Failed before: " + " | ".join(item["failed_approaches"]))
    if item.get("solution"):
        files = ", ".join(item["solution"]["files"])
        parts.append(f"Fix: {item['solution']['plan']}" + (f" (files: {files})" if files else ""))
    verification = item.get("verification") or {}
    if item.get("verified"):
        parts.append("Verified: tests passed" + (f" ({verification['result']})" if verification.get("result") else ""))
    else:
        parts.append(f"Not solved yet ({item.get('status')})")
    return ". ".join(parts)[:1500]
