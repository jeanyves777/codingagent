"""Parse human-friendly durations."""
import re


def parse_duration(text):
    """Parse durations like '1h30m', '45m', '2h' or '90s' into seconds."""
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", text):
        total += int(value) * {"h": 3600, "m": 3600, "s": 1}[unit]
    return total
