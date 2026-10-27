from urllib.parse import unquote_plus


def parse_query(qs):
    out = {}
    for part in qs.lstrip("?").split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        out.setdefault(unquote_plus(key), []).append(unquote_plus(value))
    return out
