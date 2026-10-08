"""Tag helpers."""


def add_tag(tag, tags=[]):
    """Append tag to tags (a new list when omitted) and return the list."""
    tags.append(tag)
    return tags
