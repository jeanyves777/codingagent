"""Claude (Anthropic Messages API) adapter with the same contract as OllamaModel."""
import json
from .capabilities import repository_capabilities
from .model import COORDINATOR, REVIEWER, SYSTEM, json_object

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _anthropic_tools(schemas: list[dict]) -> list[dict]:
    return [{"name": item["function"]["name"], "description": item["function"].get("description", ""),
             "input_schema": item["function"].get("parameters") or {"type": "object", "properties": {}}}
            for item in schemas]


class AnthropicModel:
    """Coordinator, implementer, and reviewer backed by Claude.

    Credentials resolve the standard way (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
    `ant auth login` profile). Server-side refusal fallbacks are enabled by default.
    """

    def __init__(self, name: str = DEFAULT_MODEL, max_tool_rounds=8, max_output_tokens=16000,
                 max_context_chars=200_000, mcp_gateway=None, effort="high", fallbacks=True,
                 client=None):
        self.name = name
        self.max_tool_rounds = max(1, min(16, max_tool_rounds))
        self.max_output_tokens = max(256, min(32768, max_output_tokens))
        self.max_context_chars = max(10_000, min(1_000_000, max_context_chars))
        self.mcp_gateway = mcp_gateway
        self.effort, self.fallbacks = effort, fallbacks
        if client is None:
            try:
                import anthropic
            except ImportError as error:
                raise RuntimeError('Install the Claude extra: pip install -e ".[claude]"') from error
            client = anthropic.AsyncAnthropic()
        self.client = client
        self.usage = []

    def _record(self, response, role):
        usage = getattr(response, "usage", None)
        self.usage.append({"role": role, "model": getattr(response, "model", self.name),
                           "prompt_tokens": getattr(usage, "input_tokens", None),
                           "output_tokens": getattr(usage, "output_tokens", None)})
        del self.usage[:-200]

    async def _create(self, role, system, messages, tools=None, max_tokens=None):
        options = {"model": self.name, "max_tokens": max_tokens or self.max_output_tokens,
                   "system": system, "messages": messages,
                   "output_config": {"effort": self.effort}}
        if tools:
            options["tools"] = tools
        if self.fallbacks:
            options["betas"] = [FALLBACK_BETA]
            options["fallbacks"] = "default"
        response = await self.client.beta.messages.create(**options)
        self._record(response, role)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ValueError("Model declined the request: " +
                             str(getattr(details, "category", None) or "unspecified"))
        if response.stop_reason == "max_tokens":
            raise ValueError("Model output budget exhausted before completion")
        return response

    @staticmethod
    def _text(response) -> str:
        return "".join(block.text for block in response.content if block.type == "text")

    async def decompose(self, goal: str) -> dict:
        response = await self._create("coordinator", COORDINATOR,
                                      [{"role": "user", "content": goal}], max_tokens=8192)
        return json_object(self._text(response))

    async def review(self, goal: str, diff: str) -> dict:
        response = await self._create("reviewer", REVIEWER,
                                      [{"role": "user", "content": json.dumps({"goal": goal, "diff": diff})}],
                                      max_tokens=8192)
        return json_object(self._text(response))

    async def propose(self, root, goal: str, memories: list[dict], repository_context=None,
                      task_id=None) -> str:
        registry = repository_capabilities(root)
        external = await self.mcp_gateway.schemas() if self.mcp_gateway else []
        tools = _anthropic_tools(registry.schemas() + external)
        messages = [{"role": "user", "content": json.dumps({
            "goal": goal, "accepted_memory": memories, "repository_context": repository_context or {}})}]
        size = len(messages[0]["content"])
        for _ in range(self.max_tool_rounds):
            if size > self.max_context_chars:
                raise ValueError("Model context character budget exceeded")
            response = await self._create("implementer", SYSTEM, messages, tools)
            calls = [block for block in response.content if block.type == "tool_use"]
            if not calls:
                text = self._text(response)
                return json.dumps(json_object(text))
            if len(calls) > 4:
                raise ValueError("Tool call budget exceeded")
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for call in calls:
                arguments = call.input if isinstance(call.input, dict) else {}
                failed = False
                try:
                    if call.name.startswith("mcp__"):
                        if not self.mcp_gateway:
                            raise PermissionError("MCP is not configured")
                        result = await self.mcp_gateway.invoke(call.name, arguments, task_id)
                    else:
                        result = registry.invoke(call.name, arguments)
                except (ValueError, KeyError, OSError, TypeError, PermissionError) as error:
                    result, failed = "Tool denied: " + str(error), True
                size += len(result)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": result,
                                "is_error": failed})
            messages.append({"role": "user", "content": results})
        raise ValueError("Model exceeded the configured planning-round budget")
