"""Pagination helpers. Pages are numbered from 1."""


def paginate(items, page, per_page):
    """Return the items shown on the given 1-based page."""
    start = (page - 1) * per_page
    return items[start:start + per_page]


def page_count(total, per_page):
    """Return how many pages are needed to show total items."""
    return -(-total // per_page)
