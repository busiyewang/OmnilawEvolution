import re

UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    if not re.fullmatch(r"(\d+[hms])+", text or ""):
        raise ValueError("bad duration: %r" % text)
    return sum(int(v) * UNITS[u] for v, u in re.findall(r"(\d+)([hms])", text))
