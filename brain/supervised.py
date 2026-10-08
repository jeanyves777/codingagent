"""Phase 2 supervision workflow: premium planning, diagnosis after repeated failures, takeover,
and human-granted escalation. The free implementer always does the routine work."""
import json
from .contracts import Proposal
from .subscriptions import SubscriptionError
from .supervision import SupervisorBudgetExceeded


def guidance_text(consultation: dict) -> str:
    result = consultation["result"]
    if consultation["kind"] == "plan":
        lines = [result.get("plan", "")] + [f"- {step}" for step in result.get("steps", [])]
        return "\nSupervisor plan from " + consultation["supervisor"] + " (follow it):\n" + "\n".join(lines)
    lines = [f"Diagnosis: {result.get('diagnosis', '')}", f"Instructions: {result.get('instructions', '')}",
             "Affected files: " + ", ".join(result.get("affected_files", [])),
             "Tests that must pass: " + ", ".join(result.get("tests", []))]
    return ("\nSupervisor repair plan from " + consultation["supervisor"] +
            " (follow it precisely):\n" + "\n".join(lines))


class SupervisionMixin:
    supervision = None

    async def _consult(self, task: dict, kind: str, payload: dict, workspace=None) -> dict | None:
        """Ask a premium supervisor, returning None (with an event) when none may or can answer."""
        if not self.supervision:
            return None
        try:
            with self.telemetry.span(task["trace_id"], "supervisor." + kind, task["id"]):
                consultation = await self.supervision.consult(task, kind, payload, workspace)
        except SupervisorBudgetExceeded as error:
            self.event(task, "supervisor_budget_exhausted",
                       f"{error}; grant one more with POST /tasks/{task['id']}/escalate")
            return None
        except SubscriptionError as error:
            self.event(task, "supervisor_unavailable", str(error)[:1000])
            return None
        task.setdefault("supervision", []).append(consultation)
        self.event(task, "supervisor_" + kind, f"{consultation['supervisor']}: " +
                   json.dumps(consultation["result"])[:1500])
        return consultation

    def complex_goal(self, task: dict, context: dict) -> bool:
        from .routing import complexity
        threshold = getattr(getattr(self, "model", None), "threshold", 4)
        return task.get("premium_plan") or complexity(task["goal"], context) >= threshold

    async def premium_plan(self, task: dict, workspace, context: dict) -> str:
        """For complex goals, let a supervisor write the plan the free worker follows."""
        if not self.supervision or not (task.get("premium_plan") or (
                self.supervision.plan_complex_tasks and self.complex_goal(task, context))):
            return ""
        consultation = await self._consult(task, "plan", {"goal": task["goal"], "repository_context": context},
                                           workspace)
        return guidance_text(consultation) if consultation else ""

    async def diagnose(self, task: dict, workspace, evidence: dict) -> dict | None:
        """After repeated free failures, get a targeted repair plan (or, if allowed, a takeover)."""
        return await self._consult(task, "diagnose", {
            "goal": task["goal"], "current_diff": task.get("diff", "")[:20_000],
            "review": task.get("review"), "evidence": evidence,
            "engineering_context": task.get("knowledge"),
            "earlier_supervision": [item["result"] for item in task.get("supervision", [])][-2:],
        }, workspace)

    def takeover_proposal(self, task: dict, consultation: dict) -> Proposal | None:
        changes = consultation["result"].get("changes") or []
        if not (self.supervision and self.supervision.takeover and changes):
            return None
        proposal = Proposal(plan="Supervisor takeover: " + consultation["result"].get("diagnosis", "")[:9000],
                            changes=changes)
        self.event(task, "supervisor_takeover",
                   f"{consultation['supervisor']} supplied {len(changes)} file(s); control returns to the "
                   "free worker after this attempt")
        return proposal

    async def final_review(self, task: dict) -> dict | None:
        if not (self.supervision and self.supervision.final_review):
            return None
        consultation = await self._consult(task, "review", {"goal": task["goal"], "diff": task["diff"]})
        return consultation["result"] if consultation else None

    def escalate(self, task_id: str) -> dict:
        """Human-approved extra premium diagnosis for a failed or blocked task."""
        task = self.store.get(task_id)
        if not self.supervision:
            raise ValueError("No supervisors are configured")
        if task.get("kind") != "task" or task["status"] not in {"failed", "blocked"}:
            raise ValueError("Only failed or blocked coding tasks can be escalated")
        if not self.workspace(task_id).exists():
            raise ValueError("Task workspace was removed; retry the task instead")
        grants = task.setdefault("supervisor_grants", {})
        grants["diagnose"] = grants.get("diagnose", 0) + 1
        task["status"] = "queued"
        self.event(task, "escalation_granted", "Human granted one supervisor diagnosis")
        self.schedule(task_id, "supervise_task")
        return task

    async def supervise(self, task_id: str):
        task = self.store.get(task_id)
        task["status"] = "planning"
        self.store.save(task)
        consultation = await self.diagnose(task, self.workspace(task_id), task.get("test_evidence") or {})
        if not consultation:
            task["status"] = "failed"
            self.event(task, "escalation_failed", "No supervisor diagnosis was available")
            return task
        proposal = self.takeover_proposal(task, consultation)
        if proposal:
            self.store_proposal(task, self.workspace(task_id), proposal, "supervisor")
            return task
        await self.plan(task, self.workspace(task_id), task["goal"] + guidance_text(consultation))
        return task
