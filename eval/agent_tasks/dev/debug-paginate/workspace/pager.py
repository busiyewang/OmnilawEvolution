def paginate(items, page, size):
    """Return the items on 1-based page `page`, `size` items per page."""
    start = page * size
    return items[start:start + size]


def page_count(n, size):
    return n // size
