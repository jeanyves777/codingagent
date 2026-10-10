"""A real client for `codingbrain api --stdio`: it starts the engine as a separate process with the
installed Python environment, speaks the JSON-lines protocol the desktop app uses, and checks the
contract from outside the process (no fakes, no in-process calls).

    python scripts/engine_client_check.py            ready handshake, info, conversation, invalid
                                                     requests, project registration, history, and a
                                                     killed engine restarted from its persisted state
    python scripts/engine_client_check.py --task     also a real task through a real model and
                                                     sandbox: start, stale-digest refusal, approve,
                                                     task-scoped live events, accept, single-use approval

Exit status 0 only if every check passed; 1 for a contract failure; 2 when the contract held but
the real model's change did not pass its tests (a capability result, reported separately). Each
step is printed.
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


class Client:
    def __init__(self, env):
        self.process = subprocess.Popen([sys.executable, "-m", "brain.local", "api", "--stdio"], env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", bufsize=1)
        self.lines = queue.Queue()
        self.events = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.stderr = []
        self.next_id = 0
        ready = self.lines.get(timeout=120)
        check(ready.get("event", {}).get("event_type") == "ready" and ready["event"].get("api_version") == "1.0",
              f"ready handshake: {ready}")

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(json.loads(line))

    def _drain_stderr(self):
        for line in self.process.stderr:
            self.stderr.append(line)

    def send_raw(self, text):
        self.process.stdin.write(text + "\n")
        self.process.stdin.flush()

    def response(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            line = self.lines.get(timeout=max(1, deadline - time.monotonic()))
            if "event" in line:
                self.events.append(line)
                continue
            return line

    def call(self, op, params=None, timeout=120, ok=True):
        self.next_id += 1
        request_id = self.next_id
        self.send_raw(json.dumps({"id": request_id, "op": op, "params": params or {}}))
        line = self.response(timeout)
        require(line.get("id") == request_id, f"{op}: response id {line.get('id')} for request {request_id}")
        if ok:
            require(line.get("ok"), f"{op}: {line.get('error')}")
            return line["result"]
        return line

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=60)

    def kill(self):
        self.process.kill()
        self.process.wait(timeout=60)


def require(condition, message):
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def model_outcome(message):
    print(f"MODEL: {message}")
    raise SystemExit(2)


def check(condition, message):
    require(condition, message)
    print(f"ok: {message}")


def git(path, *arguments):
    subprocess.run(["git", "-C", str(path), *arguments], check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", action="store_true", help="also run a real task (needs the model and sandbox)")
    parser.add_argument("--project", type=Path, help="a committed Git project for --task (default: a new one)")
    args = parser.parse_args()
    env = dict(os.environ)
    work = Path(tempfile.mkdtemp(prefix="cb-client-"))
    env.setdefault("CODINGBRAIN_HOME", str(work / "home"))
    root = args.project
    if root is None:
        root = work / "Projects" / "calculator"
        root.mkdir(parents=True)
        (root / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (root / "test_calculator.py").write_text(
            "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
        git(root.parent, "init", "-q", str(root))
        git(root, "-c", "user.name=Client Check", "-c", "user.email=client@example.com", "add", "-A")
        git(root, "-c", "user.name=Client Check", "-c", "user.email=client@example.com", "commit", "-qm", "init")

    client = Client(env)
    info = client.call("engine.info")
    check(info["api_version"] == "1.0" and info["engine_version"], f"engine.info: engine {info['engine_version']}")
    hello = client.call("conversation.send", {"message": "hello"})
    check("Coding Brain" in hello["reply"] and hello["action"] is None, "hello is answered and proposes nothing")
    for raw in ("[]", "null", "123", '"hi"', "{not json"):
        client.send_raw(raw)
        line = client.response(60)
        check(not line["ok"] and line["error"]["code"] == "bad_request", f"{raw!r} gets a structured bad_request")
    refused = client.call("conversation.send", {"message": "hi", "yes": True}, ok=False)
    check(refused["error"]["code"] == "bad_params", "there is no --yes: unknown parameters are refused")
    project = client.call("projects.register", {"path": str(root)})
    check(project["id"] and any(item["id"] == project["id"] for item in client.call("projects.list")),
          f"project registered and listed: {project['name']}")
    create = client.call("conversation.send", {"message": "Create a task management application"})
    check(create["needs"] == "confirmation" and create["action"]["kind"] == "create_project",
          "creation is proposed for confirmation, not executed")
    check(client.call("tasks.list", {"project_id": project["id"]}) == [], "conversation created no task")
    providers = {item["id"]: item for item in client.call("providers.list")}
    check(all(providers[name]["readiness"] == "not_supported" for name in ("gemini", "grok", "muse")),
          "unimplemented providers are reported as not supported")
    check(all(providers[name]["readiness"] != "ready" or providers[name]["authentication"] == "signed_in"
              for name in ("claude", "codex")), "a supervisor is ready only when signed in")

    client.kill()  # the UI or the engine dies without a goodbye
    client = Client(env)
    history = client.call("conversation.history", {})
    check(any(item.get("content") == "hello" for item in history), "conversation history survives an engine restart")
    check(any(item["id"] == project["id"] for item in client.call("projects.list")), "projects survive a restart")

    if args.task:
        run_task(client, project["id"])
    client.close()
    check(client.process.returncode == 0, "the engine exits cleanly when stdin closes")
    print("engine client check passed")


def run_task(client, project_id):
    goal = "Fix the add function in calculator.py so that it returns the sum of a and b"
    client.events.clear()
    task = client.call("tasks.start", {"project_id": project_id, "goal": goal}, timeout=1800)
    check(task["status"] in {"proposed", "failed", "blocked"}, f"tasks.start returns a governed state: {task['status']}")
    check(client.events and {line["event"]["task_id"] for line in client.events} == {task["id"]},
          "live events during tasks.start belong to this task only")
    if task["status"] != "proposed":
        model_outcome(f"the model produced no proposal ({task['status']}); failures: {task['failures']}")
    stale = client.call("tasks.approve", {"project_id": project_id, "task_id": task["id"], "digest": "0" * 64,
                                          "decision": "approve"}, ok=False)
    check(stale["error"]["code"] == "refused", "a stale digest is refused")
    client.events.clear()
    result = client.call("tasks.approve", {"project_id": project_id, "task_id": task["id"], "digest": task["digest"],
                                           "decision": "approve"}, timeout=3600)
    check(result["status"] in {"passed", "failed", "blocked"} and result["tests"] is not None,
          f"approval ran the sandbox tests: {result['status']} (exit {result['tests']['exit_code']})")
    check({line["event"]["task_id"] for line in client.events} == {task["id"]},
          "live events during tasks.approve belong to this task only")
    replay = client.call("tasks.approve", {"project_id": project_id, "task_id": task["id"], "digest": task["digest"],
                                           "decision": "approve"}, ok=False)
    check(replay["error"]["code"] == "refused", "an approval cannot be replayed")
    if result["status"] != "passed":
        model_outcome(f"the real model's change did not pass its tests; failures: {result['failures']}")
    accepted = client.call("tasks.accept", {"project_id": project_id, "task_id": task["id"]}, timeout=600)
    check(accepted["status"] == "accepted" and accepted["branch"], f"accepted onto branch {accepted['branch']}")


if __name__ == "__main__":
    main()
