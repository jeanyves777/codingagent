"""Text utilities."""
import re
import unicodedata


def shout(text):
    """Return text in upper case with an exclamation mark."""
    return text.upper() + "!"


def slugify(text):
    """Lowercase ASCII words joined by single hyphens."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
