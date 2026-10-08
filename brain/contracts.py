"""Validated contracts for proposals and delegated task graphs."""
from pydantic import BaseModel, ConfigDict, Field
from .repository import MAX_FILE


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=300)
    content: str = Field(max_length=MAX_FILE)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: str = Field(min_length=1, max_length=10_000)
    changes: list[Change] = Field(max_length=10)


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=8000)
    depends_on: list[str] = Field(default_factory=list, max_length=6)


class Delegation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignments: list[Assignment] = Field(min_length=1, max_length=6)


def validate_graph(delegation: Delegation) -> None:
    names = [item.name for item in delegation.assignments]
    if len(names) != len(set(names)):
        raise ValueError("Assignment names must be unique")
    known, graph = set(names), {}
    for item in delegation.assignments:
        dependencies = set(item.depends_on)
        if item.name in dependencies or not dependencies <= known:
            raise ValueError("Assignment dependency is invalid")
        graph[item.name] = dependencies
    visiting, visited = set(), set()

    def visit(name):
        if name in visiting:
            raise ValueError("Assignment graph contains a cycle")
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
    for name in names:
        visit(name)
