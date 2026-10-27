def remove_empty(d):
    for k in list(d):
        v = d[k]
        if isinstance(v, dict):
            remove_empty(v)
        if v is None or (isinstance(v, (str, list, dict)) and len(v) == 0):
            del d[k]
    return d
