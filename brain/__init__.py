"""Coding Brain: local engineering assistant foundation."""
try:
    from importlib.metadata import version as _version
    __version__ = _version("coding-brain")
except Exception:  # running from a source tree that is not installed
    __version__ = "0.9.0"
