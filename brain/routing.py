"""Deterministic routing that learns only from verified task outcomes."""
import sqlite3
import time
import inspect
from . import accounting
from pathlib import Path


def complexity(goal: str, context: dict | None) -> int:
    """Deterministic 0-12 complexity score from goal length and repository context size."""
    if context and isinstance(context.get("complexity"), int):
        return context["complexity"]
    symbols = len((context or {}).get("symbols", []))
    dependencies = len((context or {}).get("dependencies", []))
    return min(4, len(goal) // 1000) + min(4, symbols // 10) + min(4, dependencies // 10)


class RoutingPerformance:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS outcomes ("
                       "task_id TEXT PRIMARY KEY, model TEXT NOT NULL, success INTEGER NOT NULL, "
                       "created_at REAL NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def record(self, task_id: str, model: str, success: bool):
        with self.connect() as db:
            db.execute("INSERT INTO outcomes VALUES (?,?,?,?) ON CONFLICT(task_id) DO NOTHING",
                       (task_id, model, int(success), time.time()))

    def stats(self) -> dict[str, dict]:
        with self.connect() as db:
            rows = db.execute("SELECT model,COUNT(*),SUM(success) FROM outcomes GROUP BY model").fetchall()
        return {model: {"attempts": count, "successes": successes,
                        "success_rate": successes / count} for model, count, successes in rows}

    def choose(self, fast, strong, complex_task: bool):
        if getattr(fast, "name", None) == getattr(strong, "name", None):
            return strong, "same_model"
        stats = self.stats()
        fast_stats = stats.get(getattr(fast, "name", ""), {"attempts": 0, "successes": 0})
        strong_stats = stats.get(getattr(strong, "name", ""), {"attempts": 0, "successes": 0})
        if min(fast_stats["attempts"], strong_stats["attempts"]) < 5:
            return (strong if complex_task else fast), "heuristic_warmup"
        fast_rate = (fast_stats["successes"] + 1) / (fast_stats["attempts"] + 2)
        strong_rate = (strong_stats["successes"] + 1) / (strong_stats["attempts"] + 2)
        if complex_task:
            return (fast, "adaptive_fast") if fast_rate >= strong_rate + 0.1 else (strong, "adaptive_strong")
        return (strong, "adaptive_strong") if strong_rate >= fast_rate + 0.2 else (fast, "adaptive_fast")


class RoutedModel:
    def __init__(self, fast, strong, threshold=4, history_limit=200, performance=None):
        self.fast, self.strong = fast, strong
        self.threshold, self.history_limit = threshold, history_limit
        self.performance = performance
        self.routes = []

    def _record(self, role, model, score, task_id=None, strategy="fixed"):
        route = {"role": role, "model": getattr(model, "name", "unknown"),
                 "score": score, "strategy": strategy, "task_id": task_id}
        self.routes.append(route)
        accounting.record("route", role=role, model=route["model"], strategy=strategy)
        del self.routes[:-self.history_limit]
        return route

    def _implementer(self, goal, context, task_id=None):
        score = complexity(goal, context)
        complex_task = score >= self.threshold
        if self.performance:
            model, strategy = self.performance.choose(self.fast, self.strong, complex_task)
        else:
            model, strategy = (self.strong if complex_task else self.fast), "heuristic"
        self._record("implementer", model, score, task_id, strategy)
        return model

    async def propose(self, root, goal, memories, repository_context=None, task_id=None):
        model = self._implementer(goal, repository_context, task_id)
        parameters = inspect.signature(model.propose).parameters.values()
        supports_task = ("task_id" in inspect.signature(model.propose).parameters or
                         any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters))
        kwargs = {"task_id": task_id} if supports_task else {}
        return await model.propose(root, goal, memories, repository_context, **kwargs)

    def last_route(self, task_id):
        return next((route for route in reversed(self.routes) if route.get("task_id") == task_id), None)

    def record_outcome(self, task_id, success, model_name=None):
        route = self.last_route(task_id)
        model = model_name or (route or {}).get("model")
        if self.performance and model:
            self.performance.record(task_id, model, success)

    def performance_report(self):
        return self.performance.stats() if self.performance else {}

    async def decompose(self, goal):
        self._record("coordinator", self.strong, len(goal) // 1000)
        return await self.strong.decompose(goal)

    async def review(self, goal, diff):
        self._record("reviewer", self.strong, len(diff) // 5000)
        return await self.strong.review(goal, diff)
