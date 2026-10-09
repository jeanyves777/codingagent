"""Running the stages of a registry."""
import time

from .registry import Registry
from .report import Report


class Runner:
    def __init__(self, registry: Registry, max_attempts: int = 3, sleep=time.sleep, backoff: float = 0.5):
        self.registry, self.max_attempts, self.sleep, self.backoff = registry, max_attempts, sleep, backoff

    def run(self, context: dict) -> Report:
        report = Report()
        for stage in self.registry.order():
            if any(report.status(dependency) != "succeeded" for dependency in stage.depends_on):
                report.record(stage.name, "blocked")
                continue
            for attempt in range(1, self.max_attempts + 1):
                report.attempts[stage.name] = attempt
                try:
                    value = stage.run(context)
                except Exception as error:
                    if attempt == self.max_attempts:
                        report.record(stage.name, "failed", error=str(error))
                    else:
                        self.sleep(self.backoff * 2 ** (attempt - 1))
                    continue
                context[stage.name] = value
                report.record(stage.name, "succeeded", value=value)
                break
        return report
