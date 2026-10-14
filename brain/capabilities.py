"""Deny-by-default capability registry exposed to coding models."""
import contextvars
from dataclasses import dataclass
from typing import Callable
from .repository import inspect


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], str]
    mutating: bool = False
    approval_required: bool = False


class CapabilityRegistry:
    def __init__(self):
        self._items = {}

    def register(self, capability: Capability):
        if capability.name in self._items:
            raise ValueError(f"Duplicate capability: {capability.name}")
        self._items[capability.name] = capability

    def schemas(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": item.name, "description": item.description,
            "parameters": item.parameters,
        }} for item in self._items.values()]

    def invoke(self, name: str, arguments: dict, approved=False) -> str:
        item = self._items.get(name)
        if item is None:
            raise ValueError("Unknown capability")
        if (item.mutating or item.approval_required) and not approved:
            raise PermissionError("Capability requires explicit approval")
        if not isinstance(arguments, dict):
            raise ValueError("Capability arguments must be an object")
        required = item.parameters.get("required", [])
        properties = item.parameters.get("properties", {})
        if any(key not in arguments for key in required) or any(key not in properties for key in arguments):
            raise ValueError("Capability arguments do not match schema. Required: " + ', '.join(required) +
                             ". Allowed: " + ', '.join(properties))
        from .accounting import emit
        import time
        target = str(arguments.get('path', ''))[:200] if name == 'read_file' else ''
        description = name + (': ' + target if target else '')
        if name == 'read_file' and type(arguments.get('start_line')) is int:
            description += f" (from line {arguments['start_line']})"
        started = time.monotonic()
        emit('tool', status='RUNNING', agent='implementer', summary='Inspecting ' + description)
        try:
            result = item.handler(arguments)
        except Exception as error:
            emit('tool', status='FAILED', agent='implementer', duration=time.monotonic() - started,
                 summary=description + ' failed: ' + type(error).__name__)
            raise
        emit('tool', status='COMPLETED', agent='implementer', duration=time.monotonic() - started,
             summary='Finished ' + description)
        return result


# Task-scoped, read-only capabilities (knowledge search, symbol lookup) set by the orchestrator
# around a single proposal; adapters pick them up without changing their signatures.
TASK_CAPABILITIES: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "coding_brain_task_capabilities", default=())


# The current task's goal, used to expose only relevant external (MCP) tools.
TASK_QUERY: contextvars.ContextVar[str] = contextvars.ContextVar("coding_brain_task_query", default="")


def read_only(name: str, description: str, properties: dict, handler) -> Capability:
    return Capability(name=name, description=description, handler=handler, parameters={
        "type": "object", "properties": properties, "required": list(properties),
        "additionalProperties": False})


def repository_capabilities(root) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    def workflow_evidence(_arguments):
        import json
        from .local.lifecycle import evidence
        packet = evidence(root)
        sources = packet.get('sources', {})
        packet['source_files'] = list(sources)
        packet['sources'] = {name: text[:350] for name,text in list(sources.items())[:6]}
        packet['instruction'] = 'Excerpts are incomplete. Read relevant file ranges before choosing exact commands.'
        return json.dumps(packet)
    registry.register(read_only('project_workflow',
        'Inspect manifests, build/run scripts and documentation to infer a workflow from the project and goal; '
        'does not run commands or change files.', {}, workflow_evidence))
    definitions = [
        ("list_files", "List allowed source paths", {}),
        ("read_file", "Read a bounded line range of source; start_line is 1-based", {"path": {"type": "string"},
             "start_line": {"type":"integer","minimum":1}, "max_lines":{"type":"integer","minimum":1,"maximum":200}}),
        ("search", "Literal search across allowed source files", {"query": {"type": "string"}}),
    ]
    for name, description, properties in definitions:
        registry.register(Capability(
            name=name, description=description,
            parameters={"type": "object", "properties": properties, "required": ['path'] if name == 'read_file' else list(properties),
                        "additionalProperties": False},
            handler=lambda arguments, tool=name: inspect(root, tool, arguments),
        ))
    for capability in TASK_CAPABILITIES.get():
        if capability.mutating or capability.approval_required:
            raise ValueError("Task capabilities must be read-only")
        registry.register(capability)
    return registry
