def paginate(items, page, size):
    start = (page - 1) * size
    return items[start:start + size] if page >= 1 else []


def page_count(n, size):
    return (n + size - 1) // size
