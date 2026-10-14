"""Validated contracts for proposals and delegated task graphs."""
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from .repository import MAX_FILE


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=300)
    content: str = Field(max_length=MAX_FILE)


class LifecycleStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal['prepare', 'build', 'verify', 'apply', 'run']
    environment: Literal['sandbox', 'local'] = 'sandbox'
    command: list[str] = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=1000)
    evidence: list[str] = Field(default_factory=list, max_length=12)
    outputs: list[str] = Field(default_factory=list, max_length=12)
    timeout: int = Field(default=300, ge=1, le=3600)
    background: bool = False
    sandbox_image: str | None = Field(default=None, max_length=200, pattern=r'^[A-Za-z0-9][A-Za-z0-9._/:@-]*$')


class LifecyclePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: str = Field(min_length=1, max_length=2000)
    target: Literal['artifact', 'running', 'installed'] = 'artifact'
    steps: list[LifecycleStep] = Field(default_factory=list, max_length=16)
    preparation: list[LifecycleStep] = Field(default_factory=list, max_length=8)
    sandbox_image: str | None = Field(default=None, max_length=200, pattern=r'^[A-Za-z0-9][A-Za-z0-9._/:@-]*$')

    @model_validator(mode='after')
    def complete_workflow(self):
        if any(s.stage not in {'prepare', 'verify'} or s.background for s in self.preparation):
            raise ValueError('Preparation may set up dependencies and verify them; it cannot apply or launch the result')
        stages = [step.stage for step in self.steps]
        if not stages:
            return self
        if self.target == 'artifact' and not any(s.stage == 'build' and s.outputs for s in self.steps):
            raise ValueError('An artifact goal needs a build step with actual output file/directory paths')
        if self.target == 'running' and 'run' not in stages:
            raise ValueError('A running goal needs a run step')
        if self.target == 'installed' and 'apply' not in stages:
            raise ValueError('An installed goal needs an apply step; passing code tests does not install the result')
        if self.target in {'running', 'installed'}:
            action = 'run' if self.target == 'running' else 'apply'
            index = stages.index(action)
            if 'verify' not in stages[index + 1:]:
                raise ValueError('The running/installed result needs a subsequent verification step')
        return self


class Edit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=300)
    old: str | None = Field(default=None, min_length=1, max_length=16000)
    new: str = Field(max_length=16000)
    start_line: int | None = Field(default=None, ge=1, le=1000000)
    end_line: int | None = Field(default=None, ge=1, le=1000000)

    @model_validator(mode='after')
    def location(self):
        if self.old is not None:
            if self.start_line is not None or self.end_line is not None:
                raise ValueError('Use an exact old snippet OR an inspected line range')
        elif self.start_line is None or self.end_line is None or self.end_line < self.start_line:
            raise ValueError('An edit requires a nonempty old snippet or a valid start_line/end_line range')
        return self


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: str = Field(min_length=1, max_length=10_000)
    changes: list[Change] = Field(max_length=10)
    edits: list[Edit] = Field(default_factory=list, max_length=20)
    lifecycle: LifecyclePlan | None = None


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
