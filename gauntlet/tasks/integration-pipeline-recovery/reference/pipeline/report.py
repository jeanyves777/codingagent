"""The outcome of a pipeline run."""
from dataclasses import dataclass, field


@dataclass
class Report:
    results: dict = field(default_factory=dict)  # stage name -> {"status": ..., "value" or "error": ...}
    attempts: dict = field(default_factory=dict)  # stage name -> attempts made

    def record(self, name: str, status: str, **details) -> None:
        self.results[name] = {"status": status, **details}

    def status(self, name: str) -> str:
        return self.results[name]["status"]

    @property
    def ok(self) -> bool:
        return all(result["status"] == "succeeded" for result in self.results.values())
