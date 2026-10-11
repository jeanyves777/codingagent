"""Probe the *installed* Coding Brain engine's actual stdio API, not its version string.

Launched from the per-user Windows installer. This is a read-only preflight:
engine.info must respond through the same interpreter and transport used by the
native desktop. No model calls, project actions or provider sign-ins occur.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time


REQUIRED = {
    "conversation.send",
    "projects.create",
    "tasks.start",
    "tasks.approve",
    "tasks.accept",
}


def probe(python: str, expected_version: str | None = None, timeout: float = 50) -> tuple[bool, str]:
    if timeout <= 0:
        return False, "invalid probe timeout"
    environment = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    process = None
    stderr_tail: list[str] = []
    lines: queue.Queue = queue.Queue()

    def read_stdout(stream):
        try:
            for line in stream:
                try:
                    lines.put(json.loads(line))
                except ValueError:
                    lines.put({"invalid": "non-JSON engine response"})
        except (OSError, ValueError):
            pass
        lines.put(None)

    def read_stderr(stream):
        try:
            for line in stream:
                stderr_tail.append(line[-300:])
                if len(stderr_tail) > 20:
                    del stderr_tail[0]
        except (OSError, ValueError):
            pass

    try:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        process = subprocess.Popen(
            [python, "-I", "-m", "brain.local", "api", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=environment,
            **kwargs,
        )
        threading.Thread(target=read_stdout, args=(process.stdout,), daemon=True).start()
        threading.Thread(target=read_stderr, args=(process.stderr,), daemon=True).start()
        deadline = time.monotonic() + timeout
        ready = False
        request_sent = False
        while time.monotonic() < deadline:
            try:
                response = lines.get(timeout=min(1, max(0.05, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    return False, f"engine exited before responding (code {process.returncode})"
                continue
            if response is None:
                return False, f"engine output closed before response (code {process.poll()})"
            if not isinstance(response, dict) or response.get("invalid"):
                return False, "engine output was not a JSON object"
            if not ready:
                event = response.get("event") or {}
                if event.get("event_type") != "ready":
                    return False, "engine did not send the API ready handshake"
                if not str(event.get("api_version", "")).startswith("1."):
                    return False, "engine API major version is not compatible with desktop API 1"
                ready = True
                process.stdin.write(json.dumps({"id": "installer-probe", "op": "engine.info", "params": {}}) + "\n")
                process.stdin.flush()
                request_sent = True
                continue
            if response.get("id") != "installer-probe":
                # An API journal event is not a response. Do not mistake it for a successful probe.
                continue
            if response.get("ok") is not True:
                return False, "engine.info request was rejected"
            info = response.get("result")
            if not isinstance(info, dict) or not str(info.get("api_version", "")).startswith("1."):
                return False, "engine.info returned unsupported metadata"
            version = info.get("engine_version")
            if expected_version and version != expected_version:
                return False, f"engine reports version {version!r}, expected {expected_version!r}"
            operations = info.get("operations")
            if not isinstance(operations, list):
                return False, "engine.info has no operation list"
            missing = sorted(REQUIRED - set(op for op in operations if isinstance(op, str)))
            if missing:
                return False, "installed engine API is missing: " + ", ".join(missing)
            return True, f"engine {version} supports API 1 conversational chat and coding lifecycle"
        return False, "engine API did not respond before timeout"
    except (OSError, ValueError, BrokenPipeError) as error:
        return False, f"engine probe could not complete ({type(error).__name__})"
    finally:
        if process is not None:
            try:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=5)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify installed Coding Brain conversational engine capabilities")
    parser.add_argument("--python", required=True, help="Absolute path to installed engine Python")
    parser.add_argument("--expected-version", help="Version packaged with the current desktop installer")
    parser.add_argument("--timeout", type=float, default=50)
    arguments = parser.parse_args(argv)
    ok, message = probe(arguments.python, arguments.expected_version, arguments.timeout)
    print(("PASS: " if ok else "FAIL: ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
