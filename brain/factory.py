"""Environment-based runtime construction shared by API and workers."""
import os
from pathlib import Path
from .approvals import ToolApprovalStore
from .durable_queue import DurableQueue
from .mcp_gateway import MCPGateway
from .memory import OllamaEmbedder, PostgresVectorMemory, SemanticMemory, SQLiteVectorMemory
from .model import OllamaModel
from .routing import RoutedModel, RoutingPerformance
from .service import Brain
from .telemetry import Telemetry


def enabled(name: str, default=False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false", "1", "0"}:
        raise RuntimeError(f"{name} must be true or false")
    return value.lower() in {"true", "1"}


def build_brain_from_env(require_queue=False) -> Brain:
    model_name = os.environ.get("BRAIN_MODEL")
    if not model_name:
        raise RuntimeError("Set BRAIN_MODEL to an installed Ollama tool-capable model")
    model_url = os.environ.get("BRAIN_MODEL_URL", "http://localhost:11434")
    options = {"max_tool_rounds": int(os.environ.get("BRAIN_MAX_TOOL_ROUNDS", "8")),
               "max_output_tokens": int(os.environ.get("BRAIN_MAX_OUTPUT_TOKENS", "8192")),
               "max_context_chars": int(os.environ.get("BRAIN_MAX_CONTEXT_CHARS", "200000"))}
    data = Path(os.environ.get("BRAIN_DATA", "brain-data"))
    approvals = ToolApprovalStore(data / "tool-approvals.sqlite3")
    telemetry = Telemetry(data / "telemetry.sqlite3")
    mcp_config = os.environ.get("BRAIN_MCP_CONFIG")
    gateway = (MCPGateway.from_file(Path(mcp_config), approvals=approvals)
               if mcp_config else None)
    strong = OllamaModel(model_url, model_name, mcp_gateway=gateway, **options)
    fast = OllamaModel(model_url, os.environ.get("BRAIN_FAST_MODEL", model_name),
                       mcp_gateway=gateway, **options)
    performance = RoutingPerformance(data / "routing.sqlite3")
    implementer = RoutedModel(fast, strong, int(os.environ.get("BRAIN_STRONG_THRESHOLD", "4")),
                              performance=performance)
    memory = None
    embedding_model = os.environ.get("BRAIN_EMBEDDING_MODEL")
    if embedding_model:
        dimensions = int(os.environ.get("BRAIN_EMBEDDING_DIMENSIONS", "768"))
        embedder = OllamaEmbedder(model_url, embedding_model, dimensions)
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
        reviewer=OllamaModel(model_url, os.environ.get("BRAIN_REVIEW_MODEL", model_name)),
        coordinator=OllamaModel(model_url, os.environ.get("BRAIN_COORDINATOR_MODEL", model_name)),
        memory=memory, queue=queue, approvals=approvals, telemetry=telemetry,
        workers=max(1, min(8, int(os.environ.get("BRAIN_WORKERS", "3"))))
    )
