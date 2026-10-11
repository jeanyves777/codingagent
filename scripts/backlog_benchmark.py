"""Run a real backlog end to end and measure it: real local model, real sandbox, the conductor,
independent verification, integration and (optionally) supervisor escalation and acceptance.

    python scripts/backlog_benchmark.py --model qwen2.5-coder:7b --agents 2 --report swarm.json
    python scripts/backlog_benchmark.py --model qwen2.5-coder:7b --supervisor claude --hard 1
    python scripts/backlog_benchmark.py --model qwen2.5-coder:7b --accept     # also accepts, as the user

Everything happens in a temporary Coding Brain home and project. Reported, never assumed:
- throughput (verified items per hour), completion rate, attempts and recovery per item;
- simultaneous model calls, from the activity journal (calls in flight at the same time), next
  to the model server's measured simultaneous generation (brain.local.inference);
- CPU and memory (whole machine and the model server), sampled every second;
- premium use: supervisor consultations by kind, and premium-planned attempts.
`--accept` plays the user: it accepts the combined result, which releases the dependent item.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ITEMS = [
    ("slugify", "Create slug.py with slugify(text): lowercase, every run of characters that are not letters or "
                "digits becomes one hyphen, no hyphens at either end. slugify('Hello, World!') == 'hello-world'",
     "tests/test_slug_accept.py",
     "from slug import slugify\n\n\ndef test_slugify():\n    assert slugify('Hello, World!') == 'hello-world'\n"
     "    assert slugify('  Many   spaces ') == 'many-spaces'\n    assert slugify('A--B') == 'a-b'\n"),
    ("clamp", "Create bounds.py with clamp(value, low, high): low when value is below low, high when above high, "
              "value otherwise; ValueError when low is greater than high",
     "tests/test_bounds_accept.py",
     "import pytest\n\nfrom bounds import clamp\n\n\ndef test_clamp():\n    assert clamp(5, 0, 10) == 5\n"
     "    assert clamp(-1, 0, 10) == 0\n    assert clamp(11, 0, 10) == 10\n    with pytest.raises(ValueError):\n"
     "        clamp(1, 5, 0)\n"),
    ("roman", "Create roman.py with to_roman(number) for 1..3999 using subtractive notation (4 is IV, 9 is IX, "
              "40 is XL, 90 is XC, 400 is CD, 900 is CM); ValueError outside 1..3999",
     "tests/test_roman_accept.py",
     "import pytest\n\nfrom roman import to_roman\n\n\ndef test_roman():\n    assert to_roman(1994) == 'MCMXCIV'\n"
     "    assert to_roman(3999) == 'MMMCMXCIX'\n    assert to_roman(4) == 'IV'\n    with pytest.raises(ValueError):\n"
     "        to_roman(0)\n"),
    ("words", "Create words.py with word_counts(text) returning a dict of lowercase word -> count; words are runs "
              "of letters and apostrophes, so \"Don't stop, don't\" gives {\"don't\": 2, 'stop': 1}",
     "tests/test_words_accept.py",
     "from words import word_counts\n\n\ndef test_counts():\n    assert word_counts(\"Don't stop, don't\") == "
     "{\"don't\": 2, 'stop': 1}\n    assert word_counts('A a. B') == {'a': 2, 'b': 1}\n    assert word_counts('') == {}\n"),
]
HARD = [
    ("duration", "Create duration.py with parse_duration(text) returning seconds as an int for strings made of "
                 "number+unit parts in the order d, h, m, s, each at most once, e.g. '1h30m' -> 5400, '2d' -> 172800, "
                 "'45s' -> 45, '1d2h3m4s' -> 93784. Raise ValueError for '', '1x', '1m1h' (wrong order), '1h1h' "
                 "(repeated), 'h' (no number) and '-1h'",
     "tests/test_duration_accept.py",
     "import pytest\n\nfrom duration import parse_duration\n\n\ndef test_valid():\n"
     "    assert parse_duration('1h30m') == 5400\n    assert parse_duration('2d') == 172800\n"
     "    assert parse_duration('45s') == 45\n    assert parse_duration('1d2h3m4s') == 93784\n\n\n"
     "@pytest.mark.parametrize('text', ['', '1x', '1m1h', '1h1h', 'h', '-1h', '1h 30m'])\n"
     "def test_invalid(text):\n    with pytest.raises(ValueError):\n        parse_duration(text)\n"),
]
DEPENDENT = ("titles", "Create titles.py with title_slug(title) that returns slug.slugify(title) truncated to at most "
                       "20 characters without a trailing hyphen; use the existing slug module",
             "tests/test_titles_accept.py",
             "from titles import title_slug\n\n\ndef test_title_slug():\n"
             "    assert title_slug('Hello, World!') == 'hello-world'\n"
             "    assert title_slug('A very long title for a blog post') == 'a-very-long-title-fo'\n")


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples, self.stop = [], threading.Event()

    @staticmethod
    def _cpu():
        with open("/proc/stat") as handle:
            parts = [int(value) for value in handle.readline().split()[1:]]
        return sum(parts), parts[3] + parts[4]

    @staticmethod
    def _server_rss():
        total = 0
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                if "ollama" in Path(f"/proc/{pid}/cmdline").read_text(errors="replace"):
                    total += next(int(line.split()[1]) * 1024 for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                                  if line.startswith("VmRSS"))
            except (OSError, StopIteration):
                pass
        return total

    def run(self):
        if not Path("/proc/stat").exists():
            return  # sampling is Linux-only; the run itself is not
        last = self._cpu()
        while not self.stop.wait(1.0):
            now = self._cpu()
            info = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()}
            self.samples.append((1 - (now[1] - last[1]) / max(now[0] - last[0], 1),
                                 info["MemTotal"] - info["MemAvailable"], self._server_rss()))
            last = now

    def summary(self):
        if not self.samples:
            return None
        cpu = [sample[0] for sample in self.samples]
        return {"cpu_mean": round(sum(cpu) / len(cpu), 3), "cpu_peak": round(max(cpu), 3),
                "ram_used_peak_gb": round(max(sample[1] for sample in self.samples) / 1e9, 2),
                "model_server_rss_peak_gb": round(max(sample[2] for sample in self.samples) / 1e9, 2),
                "cores": os.cpu_count()}


def peak_overlap(intervals):
    points = sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals])
    active = peak = 0
    together = total = 0.0
    previous = points[0][0] if points else 0
    for at, delta in points:
        if active >= 1:
            total += at - previous
        if active >= 2:
            together += at - previous
        active, previous = active + delta, at
        peak = max(peak, active)
    return peak, round(together / total, 3) if total else 0.0


def git(path, *arguments):
    subprocess.run(["git", "-C", str(path), *arguments], check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--items", type=int, default=4)
    parser.add_argument("--hard", type=int, default=0, help="add the hard item(s) that a small model usually fails")
    parser.add_argument("--agents", type=int, default=2)
    parser.add_argument("--supervisor", choices=["none", "claude", "codex"], default="none")
    parser.add_argument("--accept", action="store_true", help="accept the combined result as the user would")
    parser.add_argument("--minutes", type=float, default=90)
    parser.add_argument("--timeout", type=int, default=600, help="models.timeout: seconds per local model call")
    parser.add_argument("--report")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    from brain.conductor import Backlog, BrainExecutor, Conductor
    from brain.local import cli
    from brain.local import config as settings
    from brain.local.paths import Layout
    from brain.telemetry import Telemetry

    root = Path(tempfile.mkdtemp(prefix="cb-swarm-"))
    layout = Layout(root / "home").ensure()
    os.environ["CODINGBRAIN_HOME"] = str(layout.home)
    config = settings.load(layout)
    config["models"].update(model=args.model, url=args.url, timeout=args.timeout)
    config["autonomy"]["execution"] = "auto"
    if args.supervisor != "none":
        config["supervisors"][args.supervisor]["enabled"] = True
    settings.save(layout, config)
    project = root / "projects" / "demo"
    (project / "tests").mkdir(parents=True)
    (project / "README.md").write_text("# demo\n")
    (project / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n")
    git(project, "init", "-q")
    git(project, "-c", "user.name=Bench", "-c", "user.email=bench@localhost", "add", ".")
    git(project, "-c", "user.name=Bench", "-c", "user.email=bench@localhost", "commit", "-qm", "start")

    context = cli.Context(layout, project)
    backlog = Backlog(context.data / "backlog.sqlite3")
    chosen = ITEMS[:args.items] + HARD[:args.hard]
    added = {}
    for title, goal, path, content in chosen:
        added[title] = backlog.add(goal, title=title, max_attempts=3,
                                   acceptance={"files": {path: content}, "nodes": [f"{path}"]})["id"]
    if args.accept and "slugify" in added:
        title, goal, path, content = DEPENDENT
        added[title] = backlog.add(goal, title=title, depends_on=[added["slugify"]], max_attempts=3,
                                   acceptance={"files": {path: content}, "nodes": [path]})["id"]
    resources = cli._backlog_resources(argparse.Namespace(max_agents=args.agents, remeasure=False), context, measure=True)
    executor = BrainExecutor(context.brain, project.name)
    conductor = Conductor(backlog, executor, auto_execute=True, stall_seconds=900, resources=resources)
    sampler = Sampler()
    sampler.start()
    started = time.time()
    deadline = started + args.minutes * 60
    rounds = []
    status = asyncio.run(conductor.run(poll=2.0, deadline=deadline))
    rounds.append({"after_seconds": round(time.time() - started), "counts": status["counts"]})
    accepted = None
    if args.accept and status.get("integration") and "stopped" not in status:
        cli.backlog_accept(context, backlog)
        accepted = backlog.integrations({"accepted"})[-1]
        status = asyncio.run(conductor.run(poll=2.0, deadline=deadline))  # the dependent item now runs
        rounds.append({"after_seconds": round(time.time() - started), "counts": status["counts"]})
    seconds = time.time() - started
    sampler.stop.set()
    sampler.join()

    attempts = backlog.attempts()
    brain = context.brain
    telemetry = Telemetry(context.data / "telemetry.sqlite3")
    events = telemetry.journal([attempt["task_id"] for attempt in attempts], limit=100000)
    calls = [(event["at"] - (event["duration"] or 0), event["at"], event["provider"], event["task_id"])
             for event in events if event["event_type"] == "model_response" and event["duration"]]
    local = [(start, end) for start, end, provider, _ in calls if provider == "ollama"]
    peak, together = peak_overlap(local)
    tokens = 0
    supervisor_kinds = {}
    for attempt in attempts:
        task = brain.store.get(attempt["task_id"])
        tokens += sum(entry.get("output_tokens") or 0 for entry in task.get("inference_log", [])
                      if entry.get("type") == "inference" and entry.get("provider") == "ollama")
        for entry in task.get("supervision", []):
            supervisor_kinds[entry["kind"]] = supervisor_kinds.get(entry["kind"], 0) + 1
    items = backlog.list()
    finished = [item for item in items if item["state"] in {"verified", "done"}]
    report = {
        "model": args.model, "agents_requested": args.agents, "local_slots": resources.local_slots(),
        "server_simultaneous_generations": resources.inference, "seconds": round(seconds), "rounds": rounds,
        "stopped": status.get("stopped"),
        "completion_rate": round(len(finished) / len(items), 3) if items else 0.0,
        "verified_or_done": len(finished), "items": len(items),
        "throughput_items_per_hour": round(len(finished) / seconds * 3600, 2) if seconds else 0.0,
        "per_item": [{"title": item["title"], "state": item["state"], "attempts": item["attempts"],
                      "strategies": [a["strategy"] for a in attempts if a["item_id"] == item["id"]],
                      "outcomes": [a["outcome"] for a in attempts if a["item_id"] == item["id"]],
                      "reason": (item["reason"] or "")[:300]} for item in items],
        "model_calls": {"local": len(local), "premium": sum(1 for call in calls if call[2] != "ollama"),
                        "local_peak_in_flight": peak, "local_share_of_time_two_or_more_in_flight": together,
                        "local_output_tokens": tokens,
                        "local_output_tokens_per_second": round(tokens / seconds, 2) if seconds else 0.0},
        "premium": {"supervisor_consultations": supervisor_kinds,
                    "premium_planned_attempts": sum(1 for a in attempts if a["strategy"] == "premium_plan")},
        "integration": [{"id": record["id"], "state": record["state"], "included": record["included"],
                         "excluded": {key: value["reason"] for key, value in record["excluded"].items()},
                         "branch": record.get("branch")} for record in backlog.integrations()],
        "accepted_branch": accepted and accepted.get("branch"),
        "ledger_intact": backlog.verify_ledger(),
        "resources": sampler.summary(),
        "home": str(root) if args.keep else None,
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.report:
        Path(args.report).write_text(text)
    if not args.keep:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
