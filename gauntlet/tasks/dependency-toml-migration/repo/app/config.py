"""Reading TOML configuration files."""
from pathlib import Path

import toml


class ConfigError(Exception):
    """A configuration file could not be read."""


def load_config(path) -> dict:
    """Return the parsed TOML file at path (str or Path)."""
    path = Path(path)
    try:
        return toml.load(str(path))
    except toml.TomlDecodeError as error:
        raise ConfigError(f"{path.name}: {error}") from error
