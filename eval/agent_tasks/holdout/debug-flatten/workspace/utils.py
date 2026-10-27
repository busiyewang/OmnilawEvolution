def flatten(value):
    out = []
    for item in value:
        if type(item) == list:
            out.extend(item)
        else:
            out.append(item)
    return out
