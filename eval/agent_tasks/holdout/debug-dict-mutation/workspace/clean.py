def remove_empty(d):
    for k, v in d.items():
        if isinstance(v, dict):
            remove_empty(v)
        if v in (None, "", [], {}):
            del d[k]
    return d
