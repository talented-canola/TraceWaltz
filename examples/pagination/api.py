"""Pagination for a small list API."""


def paginate(items, page=1, size=10):
    return items[:size]
