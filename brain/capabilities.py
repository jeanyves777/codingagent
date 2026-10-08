"""Deny-by-default capability registry exposed to coding models."""
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
            raise ValueError("Capability arguments do not match schema")
        return item.handler(arguments)


def repository_capabilities(root) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    definitions = [
        ("list_files", "List allowed source paths", {}),
        ("read_file", "Read an allowed source file", {"path": {"type": "string"}}),
        ("search", "Literal search across allowed source files", {"query": {"type": "string"}}),
    ]
    for name, description, properties in definitions:
        registry.register(Capability(
            name=name, description=description,
            parameters={"type": "object", "properties": properties, "required": list(properties),
                        "additionalProperties": False},
            handler=lambda arguments, tool=name: inspect(root, tool, arguments),
        ))
    return registry
