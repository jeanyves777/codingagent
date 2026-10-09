"""Settings with defaults, merged from a configuration file."""
from dataclasses import dataclass, field

from .config import load_config

DEFAULTS = {"region": "eu-west-1", "replicas": 2, "tags": []}


@dataclass
class Settings:
    region: str
    replicas: int
    tags: list = field(default_factory=list)


def load_settings(path) -> Settings:
    data = {**DEFAULTS, **load_config(path).get("deploy", {})}
    return Settings(region=data["region"], replicas=int(data["replicas"]), tags=list(data["tags"]))
