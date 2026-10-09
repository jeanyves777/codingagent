"""Pipeline stages."""
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[dict], object]
    depends_on: tuple = field(default_factory=tuple)
