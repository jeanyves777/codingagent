"""Running the stages of a registry."""
from .registry import Registry
from .report import Report


class Runner:
    def __init__(self, registry: Registry):
        self.registry = registry

    def run(self, context: dict) -> Report:
        report = Report()
        for stage in self.registry.order():
            value = stage.run(context)
            context[stage.name] = value
            report.record(stage.name, "succeeded", value=value)
        return report
