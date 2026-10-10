"""Dependency-graph planning and event-safe orchestration transitions."""
import asyncio
import uuid
from . import accounting
from .contracts import Delegation, validate_graph


class OrchestrationMixin:
    def delegate(self, repository: str, goal: str, attachments=None) -> dict:
        source = self.repository(repository)
        if not self.manager.is_git(source):
            raise ValueError("Dependency orchestration requires a Git repository")
        group = {
            "id": uuid.uuid4().hex, "kind": "orchestration", "repository": repository,
            "goal": goal, "status": "queued", "children": [], "events": [],
            "trace_id": uuid.uuid4().hex,
        }
        if attachments:
            group["attachments"] = attachments
        with self.telemetry.span(group["trace_id"], "api.delegate", group["id"]):
            self.store.save(group)
            self.schedule(group["id"], "plan_group")
        return group

    async def _plan_group(self, group_id: str):
        group = self.store.get(group_id)
        group["status"] = "planning"
        self.event(group, "coordinator", "Creating dependency graph")
        graph, entries = None, []
        goal = group["goal"]
        if group.get("attachments"):
            from .attachments import brief
            goal += "\n\n" + brief(group["attachments"])
        if self.supervision and self.supervision.plan_orchestrations:
            consultation = await self._consult(group, "decompose", {"goal": goal})
            graph = consultation["result"] if consultation else None
        if graph is None:
            with accounting.collect(entries):
                graph = await self.coordinator.decompose(goal)
        delegation = Delegation.model_validate(graph)
        validate_graph(delegation)
        group = self.store.get(group_id)
        group.setdefault("inference_log", []).extend(entries)
        if group.get("cancel_requested"):
            group["status"] = "cancelled"
            self.event(group, "cancelled", "Stopped after dependency planning")
            return
        source = self.repository(group["repository"])
        base = self.manager._git(source, "rev-parse", "HEAD")
        integration = self.data / "orchestrations" / group_id / "integration"
        await asyncio.to_thread(self.manager.integration, source, integration, base)
        group["base_commit"] = group["integration_head"] = base
        group["integration_workspace"] = str(integration)
        for assignment in delegation.assignments:
            child = self.submit(group["repository"], assignment.goal, group_id, assignment.name,
                                assignment.depends_on, launch=False, attachments=group.get("attachments"))
            group["children"].append({"name": assignment.name, "id": child["id"],
                                      "depends_on": assignment.depends_on})
        group["status"] = "active"
        self.event(group, "delegated", f"Created {len(group['children'])} dependency-aware assignments")
        self.advance_group(group_id)

    def advance_group(self, group_id: str):
        group = self.store.get(group_id)
        if group["status"] in {"cancelled", "completed", "integration_conflict"}:
            return
        children = {child["name"]: child for child in group["children"]}
        states = {name: self.store.get(child["id"])["status"] for name, child in children.items()}
        for child in group["children"]:
            task = self.store.get(child["id"])
            if task["status"] != "waiting":
                continue
            dependency_states = [states[name] for name in child["depends_on"]]
            if any(state in {"failed", "blocked", "cancelled", "integration_conflict"}
                   for state in dependency_states):
                task["status"] = "blocked"
                self.event(task, "dependency_blocked", "An upstream assignment did not complete")
            elif all(state == "accepted" for state in dependency_states):
                task["base_commit"] = group["integration_head"]
                task["status"] = "queued"
                self.store.save(task)
                self.schedule(task["id"], "create_task")
        states = [self.store.get(child["id"])["status"] for child in group["children"]]
        if states and all(state == "accepted" for state in states):
            group["status"] = "completed"
            self.event(group, "completed", "All assignments integrated")
        elif any(state in {"failed", "blocked", "integration_conflict"} for state in states):
            group["status"] = "attention_required"
            self.store.save(group)

    async def _advance_group_async(self, group_id: str):
        self.advance_group(group_id)

    def orchestration(self, group_id: str) -> dict:
        group = self.store.get(group_id)
        if group.get("kind") != "orchestration":
            raise ValueError("Not an orchestration ID")
        result = dict(group)
        result["children"] = [{**child, "task": self.store.get(child["id"])}
                              for child in group["children"]]
        owners = {}
        for child in result["children"]:
            for change in child["task"].get("proposal", {}).get("changes", []):
                owners.setdefault(change["path"], []).append(child["id"])
        result["overlapping_paths"] = {path: ids for path, ids in owners.items() if len(ids) > 1}
        return result
