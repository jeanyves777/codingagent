"""Registering stages and ordering them by dependency."""
from .stage import Stage


class Registry:
    def __init__(self):
        self._stages = {}

    def add(self, stage: Stage) -> None:
        if stage.name in self._stages:
            raise ValueError(f"duplicate stage {stage.name}")
        self._stages[stage.name] = stage

    def get(self, name: str) -> Stage:
        return self._stages[name]

    def order(self) -> list[Stage]:
        """Stages in dependency order (registration order among independent stages)."""
        ordered, visiting, done = [], set(), set()

        def visit(name):
            if name in done:
                return
            if name in visiting:
                raise ValueError(f"cycle at {name}")
            visiting.add(name)
            for dependency in self._stages[name].depends_on:
                visit(dependency)
            visiting.discard(name)
            done.add(name)
            ordered.append(self._stages[name])
        for name in self._stages:
            visit(name)
        return ordered
