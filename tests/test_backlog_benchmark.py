"""scripts/backlog_benchmark.py reports where local model time goes, so a change to model calls can
be judged against a measured baseline."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backlog_benchmark.py"
spec = importlib.util.spec_from_file_location("backlog_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_model_time_is_split_by_purpose():
    def response(role, duration, provider="ollama"):
        return {"event_type": "model_response", "duration": duration, "provider": provider, "data": {"role": role}}
    events = [response("implementer", 30.0), response("implementer", 20.0), response("reviewer", 25.0),
              response("supervisor", 9.0, provider="anthropic"), {"event_type": "model_request", "duration": None,
                                                                   "provider": "ollama", "data": {}}]
    tasks = [{"metrics": {"requirement_generation_seconds": 20.0, "requirement_generation_output_tokens": 300},
              "inference_log": [{"type": "inference", "provider": "ollama", "role": "implementer", "output_tokens": 500},
                                {"type": "inference", "provider": "ollama", "role": "reviewer", "output_tokens": 80}]},
             {"metrics": {}}]
    purpose = benchmark.by_purpose(events, tasks)
    assert purpose["by_role"] == {"implementer": {"calls": 2, "seconds": 50.0, "output_tokens": 500},
                                  "reviewer": {"calls": 1, "seconds": 25.0, "output_tokens": 80}}
    assert purpose["requirement_generation"] == {"attempts": 1, "seconds": 20.0, "output_tokens": 300,
                                                 "share_of_local_model_seconds": 0.267}
    assert purpose["review_share_of_local_model_seconds"] == 0.333
