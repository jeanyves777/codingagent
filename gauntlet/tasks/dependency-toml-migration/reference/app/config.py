"""Reading TOML configuration files."""
import tomllib
from pathlib import Path


class ConfigError(Exception):
    """A configuration file could not be read."""


def load_config(path) -> dict:
    """Return the parsed TOML file at path (str or Path)."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path.name}: {error}") from error
