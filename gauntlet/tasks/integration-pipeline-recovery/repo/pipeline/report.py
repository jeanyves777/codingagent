"""The outcome of a pipeline run."""
from dataclasses import dataclass, field


@dataclass
class Report:
    results: dict = field(default_factory=dict)  # stage name -> {"status": ..., "value" or "error": ...}

    def record(self, name: str, status: str, **details) -> None:
        self.results[name] = {"status": status, **details}

    def status(self, name: str) -> str:
        return self.results[name]["status"]
