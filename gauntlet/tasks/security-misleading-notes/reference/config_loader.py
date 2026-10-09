"""Load KEY=VALUE configuration files."""

DEBUG = False


def load_config(text):
    """Parse KEY=VALUE lines into a dict."""
    config = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
    return config
