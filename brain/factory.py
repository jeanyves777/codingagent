"""Environment-based runtime construction shared by API and workers."""
import os
from pathlib import Path
from .approvals import ToolApprovalStore
from .brains import (DEFAULT_URLS, PROVIDERS, build_roles, build_supervision, load_brains,
                     load_supervisors, validate_url)
from .durable_queue import DurableQueue
from .mcp_gateway import MCPGateway
from .memory import OllamaEmbedder, PostgresVectorMemory, SemanticMemory, SQLiteVectorMemory
from .routing import RoutedModel, RoutingPerformance
from .service import Brain
from .web_intelligence import build_web_intelligence
from .telemetry import Telemetry


def enabled(name: str, default=False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false", "1", "0"}:
        raise RuntimeError(f"{name} must be true or false")
    return value.lower() in {"true", "1"}


def build_knowledge(data: Path):
    """The Engineering Knowledge Router is on by default; BRAIN_KNOWLEDGE=false disables it."""
    if not enabled("BRAIN_KNOWLEDGE", True):
        return None
    from .knowledge import KnowledgeLibrary, KnowledgeRouter
    path = data / "knowledge.sqlite3"
    library = KnowledgeLibrary(path) if path.exists() else None
    return KnowledgeRouter(library, int(os.environ.get("BRAIN_KNOWLEDGE_BUDGET", "6000")),
                           int(os.environ.get("BRAIN_KNOWLEDGE_SKILLS", "3")))


def single_provider_config() -> dict:
    """Express the BRAIN_PROVIDER/BRAIN_*_MODEL variables as a brains configuration."""
    provider = os.environ.get("BRAIN_PROVIDER", "ollama").lower()
    if provider not in PROVIDERS:
        raise RuntimeError("BRAIN_PROVIDER must be ollama, openai, or anthropic")
    model_name = os.environ.get("BRAIN_MODEL")
    if not model_name and provider == "anthropic":
        from .anthropic_model import DEFAULT_MODEL
        model_name = DEFAULT_MODEL
    if not model_name:
        raise RuntimeError("Set BRAIN_MODEL to an installed Ollama tool-capable model")
    url = os.environ.get("BRAIN_MODEL_URL", DEFAULT_URLS.get(provider))
    if provider == "openai" and not url:
        raise RuntimeError("Set BRAIN_MODEL_URL to the OpenAI-compatible endpoint, ending in /v1")
    key_env = os.environ.get("BRAIN_MODEL_API_KEY_ENV")
    if provider != "anthropic":
        validate_url(url, bool(key_env))
    effort = os.environ.get("BRAIN_ANTHROPIC_EFFORT", "high")
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise RuntimeError("BRAIN_ANTHROPIC_EFFORT must be low, medium, high, xhigh, or max")
    names = {"implementer": model_name, "fast": os.environ.get("BRAIN_FAST_MODEL", model_name),
             "reviewer": os.environ.get("BRAIN_REVIEW_MODEL", model_name),
             "coordinator": os.environ.get("BRAIN_COORDINATOR_MODEL", model_name)}
    brains = {role: {"name": role, "provider": provider, "url": url, "model": name,
                     "api_key_env": key_env, "effort": effort,
                     "fallbacks": enabled("BRAIN_ANTHROPIC_FALLBACKS", True)}
              for role, name in names.items()}
    return {"brains": brains, "roles": {role: [role] for role in names}}


def build_brain_from_env(require_queue=False) -> Brain:
    brains_file = os.environ.get("BRAIN_BRAINS_CONFIG")
    config = load_brains(Path(brains_file)) if brains_file else single_provider_config()
    if os.environ.get("BRAIN_SUPERVISORS"):
        # Quick setup: a comma-separated list of claude and/or codex, using the signed-in CLIs.
        names = [name.strip() for name in os.environ["BRAIN_SUPERVISORS"].split(",") if name.strip()]
        providers = {"claude": "claude_cli", "codex": "codex_cli"}
        if not names or any(name not in providers for name in names):
            raise RuntimeError("BRAIN_SUPERVISORS must list claude and/or codex")
        config.update(load_supervisors({"supervisors": {name: {"provider": providers[name]}
                                                        for name in names},
                                        "supervision": config.get("supervision", {})}))
    ollama_url = (os.environ.get("BRAIN_MODEL_URL") if not brains_file and
                  os.environ.get("BRAIN_PROVIDER", "ollama").lower() == "ollama" else None)
    embedding_url = os.environ.get("BRAIN_EMBEDDING_URL", ollama_url or DEFAULT_URLS["ollama"])
    options = {"max_tool_rounds": int(os.environ.get("BRAIN_MAX_TOOL_ROUNDS", "8")),
               "max_output_tokens": int(os.environ.get("BRAIN_MAX_OUTPUT_TOKENS", "8192")),
               "max_context_chars": int(os.environ.get("BRAIN_MAX_CONTEXT_CHARS", "200000"))}
    data = Path(os.environ.get("BRAIN_DATA", "brain-data"))
    approvals = ToolApprovalStore(data / "tool-approvals.sqlite3")
    telemetry = Telemetry(data / "telemetry.sqlite3")
    mcp_config = os.environ.get("BRAIN_MCP_CONFIG")
    gateway = (MCPGateway.from_file(Path(mcp_config), approvals=approvals)
               if mcp_config else None)
    roles = build_roles(config, options, gateway)
    strong, fast = roles["implementer"], roles["fast"]
    performance = RoutingPerformance(data / "routing.sqlite3")
    implementer = RoutedModel(fast, strong, int(os.environ.get("BRAIN_STRONG_THRESHOLD", "4")),
                              performance=performance)
    memory = None
    embedding_model = os.environ.get("BRAIN_EMBEDDING_MODEL")
    if embedding_model:
        dimensions = int(os.environ.get("BRAIN_EMBEDDING_DIMENSIONS", "768"))
        embedder = OllamaEmbedder(embedding_url, embedding_model, dimensions)
        dsn = os.environ.get("BRAIN_POSTGRES_DSN")
        backend = (PostgresVectorMemory(dsn, dimensions) if dsn else
                   SQLiteVectorMemory(data / "semantic-memory.sqlite3", dimensions))
        memory = SemanticMemory(embedder, backend)
    use_queue = enabled("BRAIN_DURABLE_QUEUE", False)
    if require_queue and not use_queue:
        raise RuntimeError("Set BRAIN_DURABLE_QUEUE=true before starting a durable worker")
    queue = DurableQueue(data / "queue.sqlite3") if use_queue else None
    return Brain(
        Path(os.environ.get("BRAIN_REPOSITORIES", "repositories")), data, implementer,
        {"python": os.environ.get("BRAIN_PYTHON_SANDBOX_IMAGE",
                                  os.environ.get("BRAIN_SANDBOX_IMAGE", "coding-brain-sandbox:0.1")),
         "node": os.environ.get("BRAIN_NODE_SANDBOX_IMAGE", "coding-brain-node-sandbox:0.1")},
        reviewer=roles["reviewer"], coordinator=roles["coordinator"],
        memory=memory, queue=queue, approvals=approvals, telemetry=telemetry,
        supervision=build_supervision(config, data),
        knowledge=build_knowledge(data),
        web=build_web_intelligence(data),
        validation_retries=int(os.environ.get("BRAIN_VALIDATION_RETRIES", "2")),
        max_free_attempts=int(os.environ.get("BRAIN_MAX_FREE_ATTEMPTS", "3")),
        workers=max(1, min(8, int(os.environ.get("BRAIN_WORKERS", "3"))))
    )
