import re

UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """'1h30m' -> 5400, '45s' -> 45, '2h' -> 7200."""
    total = 0
    for value, unit in re.findall(r"(\d)([hms])", text):
        total += int(value) * UNITS[unit]
    return total
