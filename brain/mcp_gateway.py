"""Allowlisted, read-only MCP capabilities for autonomous model use."""
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from mcp import Client
from .approvals import ToolApprovalStore


NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class MCPGateway:
    def __init__(self, servers: dict, client_factory=Client, output_limit=16_000,
                 approvals: ToolApprovalStore | None = None):
        self.servers, self.client_factory = {}, client_factory
        self.approvals = approvals
        self.output_limit = max(1000, min(100_000, output_limit))
        for alias, config in servers.items():
            if not NAME.fullmatch(alias) or not isinstance(config, dict):
                raise ValueError("Invalid MCP server alias or configuration")
            url = config.get("url", "")
            parsed = urlparse(url)
            localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError("MCP URLs cannot contain credentials or fragments")
            if parsed.scheme != "https" and not (parsed.scheme == "http" and localhost):
                raise ValueError("MCP server must use HTTPS or loopback HTTP")
            tools = config.get("tools", {})
            if not isinstance(tools, dict) or any(not NAME.fullmatch(name) or policy not in
                                                  {"read_only", "approval_required"}
                                                  for name, policy in tools.items()):
                raise ValueError("MCP tools require an explicit policy")
            self.servers[alias] = {"url": url, "tools": tools}

    @classmethod
    def from_file(cls, path: Path, **kwargs):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if set(payload) != {"servers"} or not isinstance(payload["servers"], dict):
            raise ValueError("MCP configuration must contain only a servers object")
        return cls(payload["servers"], **kwargs)

    async def schemas(self) -> list[dict]:
        schemas = []
        for alias, config in self.servers.items():
            async with self.client_factory(config["url"], read_timeout_seconds=30) as client:
                result = await client.list_tools()
            for tool in result.tools:
                policy = config["tools"].get(tool.name)
                if policy not in {"read_only", "approval_required"}:
                    continue
                schema = {"type": "function", "function": {
                    "name": f"mcp__{alias}__{tool.name}",
                    "description": ((tool.description or f"Tool from {alias}") +
                                    (" Human approval is required before execution."
                                     if policy == "approval_required" else "")),
                    "parameters": tool.input_schema,
                }}
                if len(json.dumps(schema)) > 20_000:
                    raise ValueError("MCP tool schema exceeds the configured limit")
                schemas.append(schema)
                if len(schemas) > 50:
                    raise ValueError("MCP tool count exceeds the configured limit")
        return schemas

    async def invoke(self, qualified_name: str, arguments: dict, task_id=None) -> str:
        parts = qualified_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            raise ValueError("Invalid MCP capability name")
        _, alias, tool = parts
        config = self.servers.get(alias)
        policy = config["tools"].get(tool) if config else None
        if policy not in {"read_only", "approval_required"}:
            raise PermissionError("MCP capability is not allowlisted")
        request = None
        if policy == "approval_required":
            if not self.approvals or not task_id:
                raise PermissionError("Persistent approval context is required")
            request, replay = self.approvals.authorize(task_id, qualified_name, arguments)
            if replay is not None:
                return replay
        try:
            async with self.client_factory(config["url"], read_timeout_seconds=30) as client:
                result = await client.call_tool(tool, arguments)
        except Exception as error:
            if request:
                self.approvals.fail(request["id"], str(error))
            raise
        payload = {"is_error": result.is_error, "structured_content": result.structured_content,
                   "content": [block.model_dump(mode="json", by_alias=True) for block in result.content]}
        text = json.dumps(payload, ensure_ascii=False)
        output = ("MCP tool error: " if result.is_error else "") + text[:self.output_limit]
        if request:
            self.approvals.complete(request["id"], output)
        return output
