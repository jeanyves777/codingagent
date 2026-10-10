"""Live activity: which agent is doing what, now, persisted as it happens.

Every stage of a task writes RUNNING then COMPLETED / FAILED / BLOCKED / WAITING_APPROVAL /
CANCELLED events to the telemetry journal, with timestamps, durations, the responsible agent and
model, and evidence. Model requests add started / heartbeat / response events (brain.accounting),
so a viewer can tell an active request from a stalled one. Decision events carry the explanations
the models actually provided (plans, diagnoses, review reasons) and the observable decisions
(routing, escalation, retries); nothing is inferred or invented, and private model reasoning is
never requested or shown.
"""
import contextvars
import time
from contextlib import contextmanager

from . import accounting

PHASES = {
    "goal": "Goal received", "project_validation": "Project and Git validation", "indexing": "Repository indexing",
    "memory": "Memory and knowledge retrieval", "decomposition": "Goal decomposition",
    "supervisor_selection": "Supervisor selection", "planning": "Planning",
    "requirements": "Requirement and test generation", "proposal": "Proposal generation",
    "proposal_validation": "Proposal validation", "approval": "Approval", "implementation": "Code implementation",
    "tool": "Tool invocation", "review": "Code review", "sandbox": "Sandbox execution", "test_results": "Test results",
    "repair": "Diagnosis and repair", "retest": "Retesting", "completion_verification": "Completion verification",
    "visual_verification": "Visual verification", "acceptance": "Acceptance and Git integration",
}
# Installer steps (brain.local.installer), shown by the same live view.
PHASES.update({"install": "Installation", "install:preflight": "Preflight", "install:verify": "Readiness verification",
               "install:python": "Python", "install:git": "Git", "install:wsl": "WSL 2", "install:docker": "Docker Desktop",
               "install:ollama": "Ollama", "install:model": "Local coding model", "install:claude": "Claude Code",
               "install:codex": "Codex CLI", "install:sandbox": "Sandbox images", "install:knowledge": "Knowledge library",
               "install:ocr": "Tesseract OCR", "install:vision": "Vision model", "install:browser": "Browser"})
STATES = ("PENDING", "RUNNING", "COMPLETED", "FAILED", "BLOCKED", "RETRYING", "WAITING_APPROVAL", "CANCELLED")
PHASE: contextvars.ContextVar = contextvars.ContextVar("coding_brain_phase", default=None)

# Existing task events (Brain.event kinds) -> (phase, status, agent)
KINDS = {
    "approved": ("approval", "COMPLETED", "user"), "accepted": ("acceptance", "COMPLETED", "user"),
    "attempts_exhausted": ("repair", "FAILED", "coding_brain"), "blocked": (None, "BLOCKED", "coding_brain"),
    "cancellation_requested": (None, "CANCELLED", "user"), "cancelled": (None, "CANCELLED", "coding_brain"),
    "completed": ("acceptance", "COMPLETED", "coding_brain"),
    "completion_unverified": ("completion_verification", "FAILED", "coding_brain"),
    "completion_verified": ("completion_verification", "COMPLETED", "coding_brain"),
    "coordinator": ("decomposition", "RUNNING", "coordinator"), "delegated": ("decomposition", "COMPLETED", "coordinator"),
    "dependency_blocked": (None, "BLOCKED", "coding_brain"), "escalation_failed": ("supervisor_selection", "FAILED", "supervisor"),
    "escalation_granted": ("supervisor_selection", "COMPLETED", "user"),
    "implementer_invalid": ("proposal_validation", "FAILED", "implementer"),
    "implementer_unavailable": ("proposal", "BLOCKED", "implementer"), "integrated": ("acceptance", "COMPLETED", "coding_brain"),
    "integration_conflict": ("acceptance", "FAILED", "coding_brain"), "interrupted": (None, "BLOCKED", "coding_brain"),
    "knowledge_packet": ("memory", "COMPLETED", "coding_brain"), "mechanical_repair": ("proposal_validation", "COMPLETED", "coding_brain"),
    "memory_deferred": ("memory", "COMPLETED", "coding_brain"), "memory_fallback": ("memory", "RETRYING", "coding_brain"),
    "project_memory_unavailable": ("memory", "FAILED", "coding_brain"), "proposal_failed": ("proposal", "FAILED", "implementer"),
    "published": ("acceptance", "COMPLETED", "coding_brain"),
    "requirement_checks_written": ("requirements", "COMPLETED", "implementer"),
    "requirement_checks_skipped": ("requirements", "COMPLETED", "implementer"),
    "requirement_checks_rejected": ("requirements", "FAILED", "coding_brain"),
    "requirement_checks_discarded": ("completion_verification", "COMPLETED", "coding_brain"),
    "requirement_checks_failed": ("completion_verification", "FAILED", "coding_brain"),
    "retry": (None, "RETRYING", "user"), "review_disputed": ("review", "COMPLETED", "reviewer"),
    "review_overruled_by_tests": ("review", "COMPLETED", "coding_brain"), "reviewer": ("review", "COMPLETED", "reviewer"),
    "semantic_memory": ("memory", "COMPLETED", "coding_brain"),
    "supervisor_budget_exhausted": ("supervisor_selection", "BLOCKED", "supervisor"),
    "supervisor_takeover": ("repair", "COMPLETED", "supervisor"), "supervisor_unavailable": ("supervisor_selection", "FAILED", "supervisor"),
    "test_finished": ("test_results", "COMPLETED", "sandbox"), "tester": ("sandbox", "RUNNING", "sandbox"),
    "tool_approval_required": ("tool", "WAITING_APPROVAL", "implementer"),
    "tool_approval_granted": ("tool", "COMPLETED", "user"), "tool_approval_denied": ("tool", "BLOCKED", "user"),
    "upstream_check": ("repair", "COMPLETED", "coding_brain"), "validation_failed": ("proposal_validation", "FAILED", "coding_brain"),
    "visual_verification": ("visual_verification", "COMPLETED", "browser"),
    "visual_repairs_stopped": ("visual_verification", "BLOCKED", "coding_brain"),
    "web_preflight": ("memory", "COMPLETED", "coding_brain"), "workspace_removed": (None, "COMPLETED", "coding_brain"),
    "implementer": ("proposal", "COMPLETED", "implementer"), "supervisor": ("proposal", "COMPLETED", "supervisor"),
}


class ActivityMixin:
    """Journal helpers for the service (self.telemetry is the journal)."""

    def journal_event(self, task: dict, event_type: str, *, phase=None, status=None, agent=None, provider=None,
                      model=None, duration=None, summary="", artifacts=None, data=None, dedupe=None):
        try:
            return self.telemetry.append(
                event_type, task_id=task.get("id"), parent_task_id=task.get("parent_id"), trace_id=task.get("trace_id"),
                agent=agent, provider=provider, model=model, phase=phase or PHASE.get(), status=status,
                duration=duration, summary=summary, artifacts=artifacts, data=data, dedupe=dedupe)
        except Exception:
            return None  # observability never breaks a task

    def journal_task_event(self, task: dict, kind: str, detail: str):
        phase, status, agent = KINDS.get(kind, (None, None, "coding_brain"))
        if kind.startswith("supervisor_") and kind not in KINDS:
            phase, status, agent = "supervisor_selection", "COMPLETED", "supervisor"
        self.journal_event(task, "task_event", phase=phase, status=status, agent=agent, summary=f"{kind}: {detail}",
                           data={"kind": kind})

    def sink_for(self, task: dict):
        def sink(event_type, **fields):
            phase = fields.pop("phase", None)
            self.journal_event(task, event_type, phase=phase, **fields)
        return sink

    @contextmanager
    def stage(self, task: dict, phase: str, agent: str = "coding_brain", summary: str = "", **data):
        """A stage of the task: RUNNING now, then its outcome with the elapsed time. Model calls
        and tool events inside it are attributed to this task and phase."""
        import asyncio
        started = time.monotonic()
        token = PHASE.set(phase)
        self.journal_event(task, "stage", phase=phase, status="RUNNING", agent=agent,
                           summary=summary or PHASES.get(phase, phase), data=data)
        try:
            with accounting.bind(self.sink_for(task)):
                yield
        except asyncio.CancelledError:
            self.journal_event(task, "stage", phase=phase, status="CANCELLED", agent=agent,
                               duration=time.monotonic() - started, summary=f"{PHASES.get(phase, phase)} cancelled")
            raise
        except Exception as error:
            self.journal_event(task, "stage", phase=phase, status="FAILED", agent=agent,
                               duration=time.monotonic() - started,
                               summary=f"{PHASES.get(phase, phase)} failed: {type(error).__name__}: {str(error)[:400]}")
            raise
        else:
            self.journal_event(task, "stage", phase=phase, status="COMPLETED", agent=agent,
                               duration=time.monotonic() - started, summary=f"{PHASES.get(phase, phase)} done")
        finally:
            PHASE.reset(token)

    def decision(self, task: dict, phase: str, agent: str, summary: str, source: str, **data):
        """An explanation or decision with where it came from: 'model-provided' text from a model's
        own output, or 'observed' facts recorded by Coding Brain."""
        self.journal_event(task, "decision", phase=phase, status="COMPLETED", agent=agent,
                           summary=summary, data={"source": source, **data})
