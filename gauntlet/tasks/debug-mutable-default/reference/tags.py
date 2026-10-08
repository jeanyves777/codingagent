"""Tag helpers."""


def add_tag(tag, tags=None):
    """Append tag to tags (a new list when omitted) and return the list."""
    if tags is None:
        tags = []
    tags.append(tag)
    return tags
