"""Secret redaction for anything Coding Brain writes to disk about a task (journal, snapshots)."""
import re

PATTERNS = [
    re.compile(r"sk-(?:ant-|live-|proj-)?[A-Za-z0-9_\-]{16,}"), re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), re.compile(r"AKIA[0-9A-Z]{16}"), re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b(\s*[:=]\s*|\s+bearer\s+)\S+"),
]


def redact(text: str) -> str:
    for pattern in PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value
