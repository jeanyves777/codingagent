"""Parse human-friendly durations."""
import re


def parse_duration(text):
    """Parse durations like '1h30m', '45m', '2h' or '90s' into seconds."""
    if not re.fullmatch(r"(\d+[hms])+", text or ""):
        raise ValueError(f"Not a duration: {text!r}")
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", text):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total
