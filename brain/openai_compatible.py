"""Adapter for any OpenAI-compatible chat endpoint: LM Studio, llama.cpp, vLLM, LocalAI,
Jan, or free hosted tiers. Same contract as OllamaModel."""
import json
import httpx
from . import accounting
from .capabilities import repository_capabilities
from .model import SYSTEM, COORDINATOR, REVIEWER, json_object


class OpenAICompatibleModel:
    def __init__(self, url: str, name: str, api_key: str | None = None, max_tool_rounds=8,
                 max_output_tokens=8192, max_context_chars=200_000, mcp_gateway=None, timeout=300):
        self.url, self.name, self.api_key = url.rstrip("/"), name, api_key
        self.max_tool_rounds = max(1, min(16, max_tool_rounds))
        self.max_output_tokens = max(256, min(32768, max_output_tokens))
        self.max_context_chars = max(10_000, min(1_000_000, max_context_chars))
        self.mcp_gateway, self.timeout = mcp_gateway, timeout
        self.usage = []

    async def _chat(self, client, role, messages, tools=None, max_tokens=None) -> dict:
        body = {"model": self.name, "messages": messages, "temperature": 0, "stream": False,
                "max_tokens": max_tokens or self.max_output_tokens}
        if tools:
            body["tools"] = tools
        headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
        async with accounting.request(role, "openai", self.name):
            response = await client.post(self.url + "/chat/completions", json=body, headers=headers)
        response.raise_for_status()
        payload = response.json()
        usage = payload.get("usage") or {}
        accounting.record("inference", role=role, provider="openai", model=payload.get("model") or self.name,
                          requested=self.name, prompt_tokens=usage.get("prompt_tokens"),
                          output_tokens=usage.get("completion_tokens"))
        self.usage.append({"role": role, "model": self.name,
                           "prompt_tokens": usage.get("prompt_tokens"),
                           "output_tokens": usage.get("completion_tokens")})
        del self.usage[:-200]
        totals = self.__dict__.setdefault("totals", {"calls": 0, "prompt_tokens": 0, "output_tokens": 0})
        totals["calls"] += 1
        totals["prompt_tokens"] += usage.get("prompt_tokens") or 0
        totals["output_tokens"] += usage.get("completion_tokens") or 0
        choice = payload["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("Model output budget exhausted before completion")
        return choice["message"]

    def _client(self):
        return httpx.AsyncClient(timeout=self.timeout, trust_env=False)

    async def decompose(self, goal: str) -> dict:
        async with self._client() as client:
            message = await self._chat(client, "coordinator", [
                {"role": "system", "content": COORDINATOR}, {"role": "user", "content": goal}], max_tokens=4096)
        return json_object(message.get("content") or "")

    async def review(self, goal: str, diff: str) -> dict:
        async with self._client() as client:
            message = await self._chat(client, "reviewer", [
                {"role": "system", "content": REVIEWER},
                {"role": "user", "content": json.dumps({"goal": goal, "diff": diff})}], max_tokens=2048)
        return json_object(message.get("content") or "")

    async def propose(self, root, goal: str, memories: list[dict], repository_context=None,
                      task_id=None) -> str:
        registry = repository_capabilities(root)
        tools = registry.schemas() + (await self.mcp_gateway.schemas() if self.mcp_gateway else [])
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": json.dumps({"goal": goal, "accepted_memory": memories,
                                                               "repository_context": repository_context or {}})}]
        async with self._client() as client:
            for _ in range(self.max_tool_rounds):
                if sum(len(item.get("content") or "") for item in messages) > self.max_context_chars:
                    raise ValueError("Model context character budget exceeded")
                message = await self._chat(client, "implementer", messages, tools)
                calls = message.get("tool_calls") or []
                if not calls:
                    return json.dumps(json_object(message.get("content") or ""))
                if len(calls) > 4:
                    raise ValueError("Tool call budget exceeded")
                messages.append({"role": "assistant", "content": message.get("content") or "",
                                 "tool_calls": calls})
                for call in calls:
                    function = call["function"]
                    try:
                        arguments = function.get("arguments") or {}
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments or "{}")
                        if function["name"].startswith("mcp__"):
                            if not self.mcp_gateway:
                                raise PermissionError("MCP is not configured")
                            result = await self.mcp_gateway.invoke(function["name"], arguments, task_id)
                        else:
                            result = registry.invoke(function["name"], arguments)
                    except (ValueError, KeyError, OSError, TypeError, PermissionError) as error:
                        result = "Tool denied: " + str(error)
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                     "content": result})
        raise ValueError("Model exceeded the configured planning-round budget")
